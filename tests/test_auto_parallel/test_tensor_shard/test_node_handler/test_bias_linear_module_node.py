from faulthandler import disable
from functools import partial
from xml.dom import WrongDocumentErr

import pytest
import torch
import torch.multiprocessing as mp
import torch.nn as nn
from typing_extensions import Self

from colossalai.auto_parallel.tensor_shard.node_handler import LinearFunctionHandler, LinearModuleHandler
from colossalai.auto_parallel.tensor_shard.sharding_strategy import (
    OperationData,
    OperationDataType,
    ShardingStrategy,
    StrategiesVector,
)
from colossalai.device.device_mesh import DeviceMesh
from colossalai.fx import ColoGraphModule, ColoTracer
from colossalai.initialize import launch
from colossalai.logging import disable_existing_loggers
from colossalai.testing import assert_close, parameterize, rerun_if_address_is_in_use
from colossalai.testing.pytest_wrapper import run_on_environment_flag
from colossalai.testing.utils import parameterize
from colossalai.utils import free_port
from tests.test_auto_parallel.test_tensor_shard.test_node_handler.utils import numerical_test_for_node_strategy


class LinearModule(torch.nn.Module):

    def __init__(self, in_features, out_features, bias):
        super().__init__()
        self.linear = torch.nn.Linear(in_features, out_features, bias=bias)

    def forward(self, x):
        x = self.linear(x)
        return x


def check_linear_module_handler(rank, bias, world_size, port):
    disable_existing_loggers()
    launch(config={}, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    model = LinearModule(16, 32, bias=bias).cuda()

    physical_mesh_id = torch.arange(0, 4)
    mesh_shape = (2, 2)
    device_mesh = DeviceMesh(physical_mesh_id, mesh_shape, init_process_group=True)
    input = torch.rand(2, 2, 4, 16).cuda()
    # the index of linear node in computation graph
    node_index = 3
    # strategy number of linear node
    strategy_number = 10
    # construct input args
    input_args = [input]
    # construct meta arg names
    meta_arg_names = ['x']
    numerical_test_for_node_strategy(model=model,
                                     device_mesh=device_mesh,
                                     node_index=node_index,
                                     strategy_number=strategy_number,
                                     input_args=input_args,
                                     meta_arg_names=meta_arg_names,
                                     node_type='bias_module')

    tracer = ColoTracer()
    graph = tracer.trace(model, meta_args={"x": torch.rand(2, 2, 4, 16).to('meta')})
    gm = ColoGraphModule(model, graph)

    linear_mod_node = list(graph.nodes)[3]
    strategies_vector = StrategiesVector(linear_mod_node)

    # build handler
    handler = LinearFunctionHandler(node=linear_mod_node, device_mesh=device_mesh, strategies_vector=strategies_vector)
    # check operation data mapping
    mapping = handler.get_operation_data_mapping()

    for name, op_data in mapping.items():
        op_data: OperationData
        # make sure they have valid values
        assert op_data.logical_shape is not None
        assert op_data.data is not None

    assert mapping['input'].name == "x"
    assert mapping['input'].data.shape == torch.Size([2, 2, 4, 16])
    assert mapping['input'].type == OperationDataType.ARG
    assert mapping['input'].logical_shape == torch.Size([16, 16])

    assert mapping['other'].name == "linear_weight"
    assert mapping['other'].data.shape == torch.Size([32, 16])
    assert mapping['other'].type == OperationDataType.PARAM
    assert mapping['other'].logical_shape == torch.Size([16, 32])

    assert 'bias' not in mapping

    assert mapping['output'].name == "linear"
    assert mapping['output'].data.shape == torch.Size([2, 2, 4, 32])
    assert mapping['output'].type == OperationDataType.OUTPUT

    strategies_vector = handler.register_strategy(compute_resharding_cost=False)
    strategy_name_list = [val.name for val in strategies_vector]
    # one strategy will be converted to different physical sharding spec
    assert len(strategy_name_list) > 8

    # SS = SR x RS
    assert 'S0S1 = S0R x RS1' in strategy_name_list
    assert 'S1S0 = S1R x RS0' in strategy_name_list

    # SR = SS x SR
    assert 'S0R = S0S1 x S1R' in strategy_name_list
    assert 'S1R = S1S0 x S0R' in strategy_name_list

    # RS = RS x SS
    assert 'RS0 = RS1 x S1S0' in strategy_name_list
    assert 'RS1 = RS0 x S0S1' in strategy_name_list

    # RR = RS x SR
    assert 'RR = RS0 x S0R' in strategy_name_list
    assert 'RR = RS1 x S1R' in strategy_name_list

    # RS= RR x RS
    assert 'RS0 = RR x RS0' in strategy_name_list
    assert 'RS1 = RR x RS1' in strategy_name_list

    for strategy in strategies_vector:
        strategy: ShardingStrategy
        input_sharding_spec = strategy.get_sharding_spec_by_name('x')
        weight_sharding_spec = strategy.get_sharding_spec_by_name('linear_weight')
        output_sharding_spec = strategy.get_sharding_spec_by_name('linear')

        # make sure the sharding matches across different operation data
        assert input_sharding_spec.sharding_sequence[:-1] == output_sharding_spec.sharding_sequence[:-1]
        assert weight_sharding_spec.sharding_sequence[1] == input_sharding_spec.sharding_sequence[-1]
        assert weight_sharding_spec.sharding_sequence[0] == output_sharding_spec.sharding_sequence[-1]


@run_on_environment_flag(name='AUTO_PARALLEL')
@pytest.mark.dist
@rerun_if_address_is_in_use()
def test_linear_handler(bias=True):
    world_size = 4
    run_func_module = partial(check_linear_module_handler, bias=bias, world_size=world_size, port=free_port())
    mp.spawn(run_func_module, nprocs=world_size)


if __name__ == '__main__':
    test_linear_handler()
