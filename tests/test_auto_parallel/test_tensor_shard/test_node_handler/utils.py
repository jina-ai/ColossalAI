import copy
from typing import Dict, List

import torch
from torch.fx import GraphModule

from colossalai.auto_parallel.passes.runtime_apply_pass import runtime_apply_pass
from colossalai.auto_parallel.passes.runtime_preparation_pass import runtime_preparation_pass
from colossalai.auto_parallel.tensor_shard.solver import SolverOptions, StrategiesConstructor
from colossalai.auto_parallel.tensor_shard.solver.cost_graph import CostGraph
from colossalai.auto_parallel.tensor_shard.solver.graph_analysis import GraphAnalyser
from colossalai.auto_parallel.tensor_shard.solver.solver import Solver
from colossalai.device.device_mesh import DeviceMesh
from colossalai.fx.tracer.tracer import ColoTracer
from colossalai.tensor.shape_consistency import to_global
from colossalai.testing.comparison import assert_close, assert_close_loose


def _build_model_to_compare(model: torch.nn.Module, input_args: List[torch.Tensor],
                            input_kwargs: Dict[str, torch.Tensor], grad_dict: Dict[any, torch.Tensor]):

    model_to_compare = copy.deepcopy(model)
    args_to_compare = []
    kwargs_to_compare = {}
    for arg_index, input_tensor in enumerate(input_args):

        def wrapper(param, index):

            def hook_fn(grad):
                grad_dict[index] = grad

            param.register_hook(hook_fn)

        arg_to_compare = copy.deepcopy(input_tensor)
        arg_to_compare.requires_grad = True
        wrapper(arg_to_compare, arg_index)
        args_to_compare.append(arg_to_compare)

    for name, input_kwarg in input_kwargs.items():

        def wrapper(param, name):

            def hook_fn(grad):
                grad_dict[name] = grad

            param.register_hook(hook_fn)

        kwarg_to_compare = copy.deepcopy(input_kwarg)
        kwarg_to_compare.requires_grad = True
        wrapper(kwarg_to_compare, name)
        kwargs_to_compare[name] = kwarg_to_compare

    return model_to_compare, args_to_compare, kwargs_to_compare


def numerical_test_for_node_strategy(model: torch.nn.Module,
                                     device_mesh: DeviceMesh,
                                     node_index: int,
                                     strategy_number: int,
                                     input_args: List[torch.Tensor],
                                     meta_arg_names: List[str],
                                     input_kwargs: Dict[str, torch.Tensor] = {},
                                     node_type: str = 'normal'):
    for strategy_index in range(strategy_number):
        print(f'#strategy_index: {strategy_index}')
        # We need to copy the model to avoid do backward more than once in same graph
        grad_to_compare_dict = {}
        grad_to_shard_dict = {}
        model_to_compare, args_to_compare, kwargs_to_compare = _build_model_to_compare(
            model, input_args, input_kwargs, grad_to_compare_dict)
        model_to_shard, args_to_shard, kwargs_to_shard = _build_model_to_compare(model, input_args, input_kwargs,
                                                                                 grad_to_shard_dict)

        tracer = ColoTracer()
        input_sample = {}
        for input_arg, meta_arg_name in zip(input_args, meta_arg_names):
            input_sample[meta_arg_name] = torch.rand(input_arg.shape).to('meta')
        for meta_kwarg_name, input_kwarg in input_kwargs.items():
            input_sample[meta_kwarg_name] = torch.rand(input_kwarg.shape).to('meta')
        graph = tracer.trace(root=model_to_shard, meta_args=input_sample)
        gm = GraphModule(model_to_shard, graph, model_to_shard.__class__.__name__)
        solver_options = SolverOptions()
        strategies_constructor = StrategiesConstructor(graph, device_mesh, solver_options)
        strategies_constructor.build_strategies_and_cost()
        target_node = list(graph.nodes)[node_index]
        if node_type == 'normal':
            solution_len = len(strategies_constructor.leaf_strategies)
            solution = [0] * solution_len
            solution[node_index] = strategy_index
        else:
            node_vector = strategies_constructor.leaf_strategies[node_index]
            strategy_to_keep = node_vector[strategy_index]
            node_vector = [strategy_to_keep]
            # solution construction
            cost_graph = CostGraph(strategies_constructor.leaf_strategies)
            cost_graph.simplify_graph()
            graph_analyser = GraphAnalyser(gm)
            solver = Solver(gm.graph, strategies_constructor, cost_graph, graph_analyser, verbose=False)
            ret = solver.call_solver_serialized_args()
            solution = list(ret[0])
        gm, sharding_spec_dict, origin_spec_dict, comm_actions_dict = runtime_preparation_pass(
            gm, solution, device_mesh)
        gm = runtime_apply_pass(gm)
        gm.recompile()

        # forward result compare
        output = gm(*args_to_shard,
                    sharding_spec_convert_dict=sharding_spec_dict,
                    origin_node_sharding_spec_dict=origin_spec_dict,
                    comm_actions_dict=comm_actions_dict,
                    **kwargs_to_shard)
        output_to_compare = model_to_compare(*args_to_compare, **kwargs_to_compare)
        assert_close_helper(output, output_to_compare, strategy_index=strategy_index, type='forward output')

        # backward result compare
        loss = output.sum()
        loss_to_compare = output_to_compare.sum()
        loss.backward()
        loss_to_compare.backward()
        for key in grad_to_shard_dict.keys():
            grad_to_shard = grad_to_shard_dict[key]
            grad_to_compare = grad_to_compare_dict[key]
            assert_close_helper(grad_to_shard, grad_to_compare, strategy_index=strategy_index, type='input grad')

        # extract the strategy used in this iter
        strategy_in_use = target_node.strategies_vector[strategy_index]
        param_to_shard_dict = dict(gm.named_parameters())
        param_to_compare_dict = dict(model_to_compare.named_parameters())
        for name in param_to_shard_dict.keys():
            param_name = name.split('.')[-1]
            if node_type == 'normal':
                param_sharding_spec = strategy_in_use.get_sharding_spec_by_name(param_name)
            else:
                if 'weight' in name:
                    param_sharding_spec = list(graph.nodes)[4].sharding_spec
                elif 'bias' in name:
                    param_sharding_spec = list(graph.nodes)[5].sharding_spec

            grad_sharded = param_to_shard_dict[name].grad
            grad_to_compare = param_to_compare_dict[name].grad
            global_grad = to_global(grad_sharded, param_sharding_spec)
            assert_close_helper(global_grad, grad_to_compare, strategy_index=strategy_index, type='param grad')


def assert_close_helper(first: torch.Tensor,
                        second: torch.Tensor,
                        rtol: float = 1e-2,
                        atol: float = 1e-2,
                        strategy_index: int = -1,
                        type: str = 'not defined'):
    """
    This method is used to check whether the average difference between two tensors is as close as expected.
    """
    # average_diff_tensor = ((first - second)/(second+0.1)).sum()/second.numel()
    try:
        assert_close(first, second, rtol=rtol, atol=atol)
    except:
        print(f'strategy index {strategy_index} encounter assert_close error on {type}')
