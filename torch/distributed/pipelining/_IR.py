# Copyright (c) Meta Platforms, Inc. and affiliates
import copy
import logging
import operator
from dataclasses import dataclass
from enum import Enum
from inspect import Parameter, signature, Signature
from types import MethodType
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
import torch.fx as fx
from torch.export import ExportedProgram
from torch.export.unflatten import _assign_attr, _AttrKind, _sink_params
from torch.fx.node import map_aggregate
from torch.fx.passes.split_module import split_module

from ._backward import _null_coalesce_accumulate, stage_backward
from ._unflatten import _outline_submodules
from .microbatch import split_args_kwargs_into_chunks, TensorChunkSpec


logger = logging.getLogger(__name__)

# TODO:
# 1. investigate gradient sync for shared parameters. how does DDP do it?
# 2. Add parameter movement to split_module


def _find_loss_from_output_and_spec(output_val, spec_val):
    if spec_val is False:
        return None
    if spec_val is True:
        if not isinstance(output_val, fx.Node):
            raise RuntimeError(
                f"Loss spec must specify a dynamic value but got {output_val}"
            )
        return output_val

    if isinstance(spec_val, (tuple, list)):
        if not isinstance(output_val, (tuple, list)):
            raise RuntimeError(
                f"Output value {output_val} must match type of loss specification "
                f"{spec_val}"
            )
        if len(output_val) != len(spec_val):
            raise RuntimeError(
                f"Output value {output_val} must match length of loss specification "
                f"{spec_val}"
            )
        for out, spec in zip(output_val, spec_val):
            loss_val = _find_loss_from_output_and_spec(out, spec)
            if loss_val is not None:
                return loss_val
        raise RuntimeError(f"Did not find loss value in specification {spec_val}")

    if isinstance(spec_val, dict):
        if not isinstance(output_val, dict):
            raise RuntimeError(
                f"Output value {output_val} must match type of loss specification "
                f"{spec_val}"
            )
        if set(output_val.keys()) != set(spec_val.keys()):
            raise RuntimeError(
                f"Output value {output_val} must match keys of loss specification "
                f"{spec_val}"
            )
        for k in spec_val:
            loss_val = _find_loss_from_output_and_spec(output_val[k], spec_val[k])
            if loss_val is not None:
                return loss_val
        raise RuntimeError(f"Did not find loss value in specification {spec_val}")

    raise RuntimeError(f"Unsupported type {type(spec_val)} in loss specification")


def _find_loss_output(mod: torch.nn.Module, g: fx.Graph, output_loss_value_spec):
    output_nodes = [n for n in g.nodes if n.op == "output"]
    assert len(output_nodes) == 1
    output_node = output_nodes[0]
    output_val = output_node.args[0]
    generated_spec: Any = None

    if isinstance(mod, TrivialLossWrapper):
        # TrivialLossWrapper is pre-defined by PiPPy.
        # It has loss as the only output so we can safely assume the first output arg is the loss.
        assert len(output_node.args) == 1
        loss_node = output_val
        generated_spec = TrivialLossWrapper.loss_spec
    elif output_loss_value_spec is None:
        # Use default spec, i.e. search for "loss" in output values
        if isinstance(output_val, dict) and "loss" in output_val.keys():
            loss_node = output_val["loss"]
            generated_spec = {k: k == "loss" for k in output_val}
        else:
            loss_node = None
            generated_spec = None
    else:
        loss_node = _find_loss_from_output_and_spec(output_val, output_loss_value_spec)
        generated_spec = output_loss_value_spec

    return loss_node, output_node, generated_spec


def _insert_stage_symbolic_backward(
    g: fx.Graph,
    loss_node: fx.Node,
    output_node: fx.Node,
):
    # Collect metadata about tuple output values. TODO: move this to split_module or FX IR
    tuples: Dict[fx.Node, Tuple] = {}
    for node in reversed(g.nodes):
        if node.op == "call_function":
            # In the forward pass, only emit placeholder, module calls, and
            # getitem calls. If we have a target other than getitem in this
            # (forward-only) code, there is a bug.
            assert node.target == operator.getitem, (
                "Found non-getitem call in forward pass. "
                "Please report a bug to PiPPy"
            )
            assert (
                len(node.args) == 2
            ), "Found malformed getitem call. Please report a bug to PiPPy"
            indexed_value, node_idx = tuple(node.args)

            # indexed_value is a collection that we are indexing into. It could
            # exist in the tuples map if we've processed another `getitem`
            # already.
            existing_list_size = (
                len(tuples[indexed_value]) if indexed_value in tuples else -1
            )
            new_list_size = max(node_idx + 1, existing_list_size)

            reconstructed_list = [None for _ in range(new_list_size)]

            # Copy over existing elements if present
            if indexed_value in tuples:
                for i, val in enumerate(tuples[indexed_value]):
                    reconstructed_list[i] = val

            # Populate value represented by this node
            reconstructed_list[node_idx] = node

            tuples[indexed_value] = tuple(reconstructed_list)

    # Keep track of nodes that dominate the loss node.
    # We will only emit backward operations for nodes that can contribute
    # to the specified loss value.
    live_nodes = {loss_node: None}
    val_to_grad: Dict[fx.Node, Optional[fx.Node]] = {loss_node: None}

    def assign_or_accumulate_grad(forward_node, grad_value):
        if forward_node in val_to_grad and forward_node.op != "placeholder":
            grad_value = g.call_function(
                _null_coalesce_accumulate,
                (val_to_grad[forward_node], grad_value),
            )
        val_to_grad[forward_node] = grad_value

    with g.inserting_before(output_node):
        for node in reversed(g.nodes):
            if node not in live_nodes:
                continue

            def add_to_live_nodes(n):
                live_nodes.setdefault(n, None)

            fx.node.map_arg(node.args, add_to_live_nodes)
            fx.node.map_arg(node.kwargs, add_to_live_nodes)
            if node.op == "call_module":
                output_grads: Union[Tuple[Optional[fx.Node], ...], Optional[fx.Node]]
                if node in tuples:
                    stage_output = tuples[node]
                    output_grads = tuple(val_to_grad.get(n, None) for n in tuples[node])
                    outputs_with_grads_idxs = [
                        i for i, n in enumerate(tuples[node]) if n in live_nodes
                    ]
                else:
                    stage_output = (node,)
                    output_grads = val_to_grad[node]
                    outputs_with_grads_idxs = [0]

                output_grads = (
                    (output_grads,)
                    if not isinstance(output_grads, tuple)
                    else output_grads
                )

                grad_call = g.call_function(
                    stage_backward,
                    kwargs={
                        "stage_output": stage_output,
                        "output_grads": output_grads,
                        "input_values": list(node.all_input_nodes),
                        "outputs_with_grads_idxs": outputs_with_grads_idxs,
                    },
                )
                # Insert backward stage debug info
                kwargs_copy = dict(grad_call.kwargs)
                grad_call.kwargs = kwargs_copy

                grad_call_proxy = fx.Proxy(grad_call)
                grads = grad_call_proxy.node

                input_nodes = list(node.all_input_nodes)
                grads_proxy = fx.Proxy(grads)
                for i, input_node in enumerate(input_nodes):
                    assign_or_accumulate_grad(input_node, grads_proxy[i].node)

    return g


class PipeSequential(torch.nn.Sequential):
    @staticmethod
    def from_sequential(sequential_instance: torch.nn.Sequential):
        return PipeSequential(*[copy.copy(m) for m in sequential_instance])

    def forward(self, input):
        for i, module in enumerate(self):
            input = module(input)
            if i != len(self) - 1:
                pipe_split()
        return input


class LossWrapper(torch.nn.Module):
    """
    LossWrapper is a convenient abstract class that allows you to wrap up both
    your model as well as its loss function and specify the connectivity between
    the inputs, model, loss function, and output value. Example::

        class MyModelWrapper(LossWrapper):
            def forward(self, x, targets):
                model_out = self.module(x)
                loss_value = self.loss_fn(model_out, targets)
                return loss_value

    The above example defines a connectivity where we expect the forward/loss/backward
    training procedure to take two arguments (x and targets), pass x into the module
    to get the output of the feedforward computation, pass the model output and the
    targets value into the loss function, and get and return the loss value, which will
    be backpropagated by PiPPy. The above class would then be instantiated like::

        model = ... # instantiate the model
        loss_fn = torch.nn.MSELoss() # for the sake of demonstration

        wrapper = MyModelWrapper(model, loss_fn)
        pipe = Pipe.from_tracing(wrapper, ...)

    """

    def __init__(self, module, loss_fn):
        super().__init__()
        self.module = module
        self.loss_fn = loss_fn

    def forward(self, *args, **kwargs):
        raise NotImplementedError(
            "This instance of LossWrapper does not have an overridden"
            "forward(). Please implement forward() to specify the arguments, "
            "connection between the module and loss, and loss output "
            "value."
        )


class TrivialLossWrapper(LossWrapper):
    def forward(self, x, targets):
        model_out = self.module(x)
        return self.loss_fn(model_out, targets)

    loss_spec = True


# Pipe model representation
#
# Pipe can be thought of as an `nn.Sequential++`. That is to say: it specifies
# a single topological ordering of pipeline "stages" that, when run in series,
# constitutes all of the operations of the program. However, unlike `nn.Sequential`,
# Pipe allows non-local usages of values, so long as those uses still respect
# topological ordering. In particular:
#
# 1. Non-local activations. This type of usage can appear in, for example, skip
#    connections. These values will be directly transmitted from the "def" stage
#    to all stages that use them skipping intermediate stages. During autograd,
#    gradients will be propagated back through this skip connection reverse
#    to how activations propagated in the forward pass.
# 2. Non-local parameter/module invocations. This occurs when a parameter is used
#    in a stage downstream of where it is resident. These values can be carried
#    forward similarly to (1), but in addition one might want to replicate the
#    value on multiple stages. Gradients for these shared parameters will be
#    accumulated separately on each stage, but there will be an additional
#    gradient accumulation before the optimizer step.


# Register `_pipe_split()` as an ATen operator. This is required for Export to
# preserve this marker in the graph.
torch.library.define("pippy::_pipe_split", "() -> ()")


@torch.library.impl("pippy::_pipe_split", "BackendSelect")
def _pipe_split():
    return None


@torch.library.register_fake("pippy::_pipe_split")  # type: ignore[no-redef]
def _pipe_split():  # noqa: F811
    return None


# Add an alias for convenience
aten_pipe_split_alias = torch.ops.pippy._pipe_split.default

# Ask Export to preserve the `_pipe_split` op.
# See examples in pytorch/torch/fx/node.py
fx.node._side_effectful_functions.add(aten_pipe_split_alias)


# User facing API
def pipe_split():
    """
    pipe_split is a special operator that is used to mark the boundary between
    stages in a module. It is used to split the module into stages. It is a
    no-op if your annotated module is run eagerly.

    Example:
        >>> # xdoctest: +SKIP
        >>> def forward(self, x):
        >>>     x = torch.mm(x, self.mm_param)
        >>>     x = torch.relu(x)
        >>>     pipe_split()
        >>>     x = self.lin(x)
        >>>     return x

    The above example will be split into two stages.
    """
    return torch.ops.pippy._pipe_split()


class MultiUseParameterConfig(Enum):
    TRANSMIT = 1
    REPLICATE = 2


MultiUseParamSpec = Union[MultiUseParameterConfig, Dict[str, MultiUseParameterConfig]]


class DetachExecutor(fx.Interpreter):
    """
    Special interpreter to run the split_gm in testing that detaches all inputs to
    a module invocation. This is needed so that the values at the boundary are
    leaf modules in autograd execution.
    """

    def __init__(self, module, garbage_collect_values=True):
        garbage_collect_values = False
        super().__init__(module, garbage_collect_values)
        self.value_remap = {}

    def run(self, *args, initial_env=None):
        self.value_remap = {}
        return super().run(*args, initial_env=initial_env)

    def call_module(self, target, args, kwargs):
        def detach_tensors(a):
            if isinstance(a, torch.Tensor) and a.requires_grad:
                if a not in self.value_remap:
                    new_val = a.detach().requires_grad_(True)
                    self.value_remap[a] = new_val
                return self.value_remap[a]
            else:
                return a

        """
        def dont_traverse_size(a):
            return type(a) != torch.Size
        """

        args = map_aggregate(
            args,
            detach_tensors,  # dont_traverse_size
        )
        kwargs = map_aggregate(
            kwargs,
            detach_tensors,  # dont_traverse_size
        )

        return super().call_module(target, args, kwargs)

    def call_function(self, target, args, kwargs):
        # HACK to reroute saved input tensors to point to the detach()ed version
        if target == stage_backward:
            kwargs = dict(kwargs)
            kwargs["input_values"] = [
                self.value_remap.get(v, v) for v in kwargs["input_values"]
            ]
        return super().call_function(target, args, kwargs)


class _NodeReference:
    def __init__(self, name):
        self.name = name

    name: str


class _LinearNodeList:
    def __init__(self, node_list):
        self.serialize_node_list = []
        for node in node_list:
            node_args = fx.node.map_arg(node.args, lambda n: _NodeReference(n.name))
            node_kwargs = fx.node.map_arg(node.kwargs, lambda n: _NodeReference(n.name))
            serialize_node = fx.Node(
                graph=None,
                name=node.name,
                op=node.op,
                target=node.target,
                args=node_args,
                kwargs=node_kwargs,
                return_type=node.type,
            )
            serialize_node.meta = copy.copy(node.meta)
            self.serialize_node_list.append(serialize_node)

    def to_graph(self):
        graph = fx.Graph()

        ref_str_to_node: Dict[str, fx.Node] = {}

        def ref_to_node(arg):
            if isinstance(arg, _NodeReference):
                return ref_str_to_node[arg.name]
            else:
                return arg

        for node in self.serialize_node_list:
            node_args = map_aggregate(node.args, ref_to_node)
            node_kwargs = map_aggregate(node.kwargs, ref_to_node)
            deser_node = graph.create_node(
                op=node.op,
                target=node.target,
                args=node_args,
                kwargs=node_kwargs,
                name=node.name,
                type_expr=node.type,
            )
            ref_str_to_node[node.name] = deser_node

        return graph


def _direct_serialization_deserialize(body, nodes):
    """
    Custom `__reduce__` method for serialization.
    DO AS I SAY -- NOT AS I DO. This violates the principle that
    GraphModules serialize via code export & re-tracing. We allow
    for this here because **PIPE STAGES SHOULD NOT BE PERSISTED
    TO DISK -- THIS IS ONLY FOR TRANSMISSION VIA RPC**. Persisting
    these instances to disk will expose internal implementation
    details of `fx.Graph` and related data structures and is
    NOT advised.
    """

    class DummyModule(torch.nn.Module):
        def __init__(self, body):
            super().__init__()
            self.__dict__.update(body)

    dummy = DummyModule(body)

    return fx.GraphModule(dummy, nodes.to_graph())


def _direct_serialization_reduce(self):
    serialization_dict = dict(self.__dict__)
    serialization_dict.pop("_graph")
    return (
        _direct_serialization_deserialize,
        (serialization_dict, _LinearNodeList(self.graph.nodes)),
    )


class Pipe(torch.nn.Module):
    # Class variables
    """
    args_chunk_spec:
        Chunking specification for positional inputs. (default: `None`)
    kwargs_chunk_spec:
        Chunking specification for keyword inputs. (default: `None`)
    """
    # args_chunk_spec and kwargs_chunk_spec are used to specify how to chunk
    # inputs. They are used to create microbatched examples before tracing.
    # See context managers `ArgsChunkSpec` and `KwargsChunkSpec`.
    # TODO: Do we need to support `_Replicate`? It's unclear, dropping for now.
    args_chunk_spec: Optional[Tuple[TensorChunkSpec, ...]] = None
    kwargs_chunk_spec: Optional[Dict[str, TensorChunkSpec]] = None

    @dataclass
    class PipeInfo:
        graph: fx.Graph
        num_stages: int
        num_chunks: int
        has_loss_and_backward: bool
        args_chunk_spec: Optional[Tuple[Any, ...]] = None
        kwargs_chunk_spec: Optional[Dict[str, Any]] = None

    def __init__(
        self,
        split_gm: fx.GraphModule,
        num_stages: int,
        has_loss_and_backward: bool,
        loss_spec,
    ):
        # TODO: is there a way not to hard wire init?
        torch.nn.Module.__init__(self)
        self.split_gm: fx.GraphModule = split_gm
        self.executor: DetachExecutor = DetachExecutor(self.split_gm)
        self.num_stages: int = num_stages
        self.has_loss_and_backward = has_loss_and_backward
        self.loss_spec = loss_spec
        self.pipe_info: Optional[Pipe.PipeInfo] = None

        for node in split_gm.graph.nodes:
            assert (
                node.op in {"call_module", "placeholder", "output"}
                or (node.op, node.target) == ("call_function", operator.getitem)
                or (node.op, node.target) == ("call_method", "backward")
                or (node.op, node.target) == ("call_function", stage_backward)
                or (node.op, node.target)
                == ("call_function", _null_coalesce_accumulate)
            ), node

        # Detect replicated parameters so we know that we have to do an additional allreduce
        # before applying the optimizer
        #
        # Note that this also handles the case where there were multiple calls to a single
        # module from different stages, regardless of whether that module invocation
        # was handled by the logic above.

        # Map parameter value to a dictionary that maps the user pipeline module
        # to the local qualname within that module
        params_to_users: Dict[torch.nn.Parameter, Dict[str, str]] = {}

        for m_qualname, mod in self.split_gm.named_children():
            for p_qualname, param in mod.named_parameters():
                params_to_users.setdefault(param, {})
                params_to_users[param][m_qualname] = p_qualname

        self.replicated_params: List[Dict[str, str]] = [
            use_mapping
            for _, use_mapping in params_to_users.items()
            if len(use_mapping) > 1
        ]

        # We must break the aliasing relationship between the replicated parameters for correct
        # numerics in reference runs. If we do not do this, the autograd tape in separate stages
        # will have a reference to the same tensor value and will erroneously apply gradient
        # updates multiple times. Therefore, for each replicated parameter set, we deepcopy the
        # values so that we have separate instances.
        for param_mapping in self.replicated_params:
            for submod_name, param_qualname in param_mapping.items():
                submod = getattr(self.split_gm, submod_name)
                atoms = param_qualname.split(".")
                for atom in atoms[:-1]:
                    submod = getattr(submod, atom)
                setattr(submod, atoms[-1], copy.deepcopy(getattr(submod, atoms[-1])))

        def throw(self, *args, **kwargs):
            raise RuntimeError(
                "To run pipeline locally, invoke the Pipe object directly, not `split_gm`"
            )

        self.split_gm.forward = throw

        # Make submodules use custom direct-serialized GraphModule
        i = 0
        while True:
            try:
                name = f"submod_{i}"
                submod = getattr(self.split_gm, name)
                submod.__class__.__reduce__ = _direct_serialization_reduce
                i += 1
            except AttributeError:
                break

    def forward(self, *args, **kwargs):
        executor_args = args
        if len(kwargs) > 0:
            parameters = []
            for node in self.split_gm.graph.nodes:
                if node.op == "placeholder":
                    if node.args and len(node.args) > 0:
                        parameters.append(
                            Parameter(
                                node.target,
                                Parameter.POSITIONAL_OR_KEYWORD,
                                default=node.args[0],
                            )
                        )
                    else:
                        parameter_kind = Parameter.POSITIONAL_OR_KEYWORD
                        param_name = node.target
                        if node.target.startswith("**"):
                            parameter_kind = Parameter.VAR_KEYWORD  # type: ignore[assignment]
                            param_name = param_name[2:]
                        elif node.target.startswith("*"):
                            parameter_kind = Parameter.VAR_POSITIONAL  # type: ignore[assignment]
                            param_name = param_name[1:]
                        parameters.append(Parameter(param_name, parameter_kind))
            signature = Signature(parameters)
            ba = signature.bind(*args, **kwargs)
            ba.apply_defaults()
            executor_args = ba.arguments.values()  # type: ignore[assignment]

        res = self.executor.run(*executor_args)

        return res

    def get_stage_module(self, stage_idx: int) -> torch.nn.Module:
        if stage_idx < 0 or stage_idx >= self.num_stages:
            raise ValueError(f"Invalid stage index {stage_idx}!")
        return getattr(self.split_gm, f"submod_{stage_idx}")

    @staticmethod
    def _number_and_count_forward_stages(gm: fx.GraphModule):
        num_stages = 0
        found_idxs: Dict[int, None] = {}
        for node in gm.graph.nodes:
            if node.op == "call_module" and node.target.startswith("submod_"):
                node.meta["stage_idx"] = int(node.target[len("submod_") :])
                found_idxs.setdefault(node.meta["stage_idx"])
                num_stages += 1

        # this assert will fail if a split point is inserted before the first layer, which creates empty first submodule
        # Update: the following assert may fail against some torch versions >=
        # 2.2.0, as:
        # submod_0, submod_1, submod_2, ...
        # may be named as
        # submod_0, submod_2, submod_4, ...
        # TODO: investigate
        # assert all(i in found_idxs for i in range(num_stages))

        return num_stages

    @staticmethod
    def _from_traced(
        mod: torch.nn.Module,
        exported_program: ExportedProgram,
        multi_use_param_spec: Optional[MultiUseParamSpec] = None,
        output_loss_value_spec=None,
        split_policy: Optional[
            Callable[[torch.fx.GraphModule], torch.fx.GraphModule]
        ] = None,
    ):
        """
        Additionally, the ``output_loss_value_spec`` value can be specified to disambiguate
        which value in the output of `forward` is the loss value on which PiPPy should apply
        backpropagation. For example, if your ``forward`` returns a tuple ``(loss, model_out)``,
        you can specify ``output_loss_value_spec=(True, False)``. Or, if your ``forward`` returns
        a dict ``{'loss': loss_value, 'model_out': model_out}``, you can specify
        ``output_loss_value_spec={'loss': True, 'model_out': False}``
        """

        traced = exported_program.module()

        if split_policy is not None:
            logger.info("Auto-splitting model")
            traced = split_policy(traced)  # type: ignore[arg-type]

        logger.debug(traced.print_readable(print_output=False))

        # Deduplicate `get_attr` nodes that refer to the same parameter . Downstream code for moving
        # parameters relies on the invariant that parameter accesses happen once. This is not necessarily
        # the case (especially with custom tracers), so fix that up here.
        get_attr_nodes: Dict[str, fx.Node] = {}
        for node in traced.graph.nodes:
            if node.op == "get_attr":
                get_attr_nodes.setdefault(node.target, node)

                if get_attr_nodes[node.target] != node:
                    node.replace_all_uses_with(get_attr_nodes[node.target])
                    traced.graph.erase_node(node)

        # avoid looking at next node by keeping track of previous pipe_split
        prev_pipe_split_idx = -1
        pipe_split_nodes_to_erase = set()
        for i, node in enumerate(traced.graph.nodes):
            if (node.op, node.target) == ("call_function", pipe_split):
                if prev_pipe_split_idx == i - 1:
                    pipe_split_nodes_to_erase.add(node)
                prev_pipe_split_idx = i

        for node in pipe_split_nodes_to_erase:
            traced.graph.erase_node(node)

        traced.recompile()

        part_idx = 0

        def split_callback(n: fx.Node):
            nonlocal part_idx
            if (n.op, n.target) == (
                "call_function",
                aten_pipe_split_alias,
            ):
                logger.debug(f"Found pipe_split {part_idx}")  # noqa: G004
                part_idx += 1
            return part_idx

        # TODO: what does split do with module invocations? does it move the modules
        # into the submodules?
        split = split_module(traced, mod, split_callback)
        # a (custom) tracer can produce dead code like orphan get_attr nodes
        split.graph.eliminate_dead_code()

        # peephole to remove pipe_split
        for submodule in split.modules():
            if isinstance(submodule, fx.GraphModule):
                for node in submodule.graph.nodes:
                    if (node.op, node.target) == (
                        "call_function",
                        aten_pipe_split_alias,
                    ):
                        submodule.graph.erase_node(node)
                submodule.recompile()

        for name, submodule in split.named_children():
            if isinstance(submodule, fx.GraphModule):
                new_submod = _outline_submodules(submodule.graph)
                # Replace old submod
                split.register_module(name, new_submod)

        # TODO: backport this into split_module
        def delete_user_reference(node, user):
            """
            Delete reference of `node` from `user`'s arg list.
            Args:
                - node: a `get_attr` node at root.
                - user: a submodule node that uses `node`.
            """
            assert len(user.kwargs) == 0
            use_idxs = [i for i, arg in enumerate(user.args) if arg == node]
            assert len(use_idxs) == 1
            args_copy = list(user.args)
            args_copy.pop(use_idxs[0])
            user.args = tuple(args_copy)
            logger.debug(
                f"Deleted {node} from user {user}, arg index = {use_idxs[0]}"  # noqa: G004
            )

        # A list of param referrals for deferred deletion.
        # To be accumulated in `move_param_to_callee`.
        to_delete = list()

        def move_param_to_callee(
            root,
            callee_name,
            param_fqn,
        ):
            """
            Move a parameter from the root module to a submodule.
            Args:
                root: The root module.
                callee_name: The name of the submodule to move the parameter to.
                param_fqn: The fully qualified name of the parameter to move.
            """
            # `atoms` is a list of strings representing the path to the
            # parameter in the original model
            atoms = param_fqn.split(".")
            # Recursively find the parent of the parameter
            mod_itr = split
            for atom in atoms[:-1]:
                mod_itr = getattr(mod_itr, atom)
            # Get the parameter (it is still under the root module)
            param_val = getattr(mod_itr, atoms[-1])
            # Check whether the parameter is a buffer or a parameter
            is_buffer = atoms[-1] in mod_itr._buffers

            # Check whether the parameter is a tensor
            assert isinstance(param_val, torch.Tensor), (
                f"Expected '{param_fqn}' to be {torch.Tensor} but got {type(param_val)}."
                + (
                    f" It might happen if module '{param_fqn}' was passed to some 'leaf function'"
                    f"(see https://pytorch.org/docs/stable/fx.html#fx.wrap). Please inspect "
                    f"usages of '{param_fqn}' in the traced graph."
                    if isinstance(param_val, torch.nn.Module)
                    else ""
                )
            )

            # Get submodule
            callee = root.get_submodule(callee_name)
            assert not hasattr(
                callee, param_fqn
            ), f"Module {callee_name} already has a parameter named {param_fqn}"

            # Assign the parameter to the submodule
            if is_buffer:
                _assign_attr(
                    param_val,
                    callee,
                    param_fqn,
                    attr_kind=_AttrKind.BUFFER,
                    persistent=True,  # TODO: handle non-persistent buffer
                )
            else:
                _assign_attr(
                    param_val,
                    callee,
                    param_fqn,
                    attr_kind=_AttrKind.PARAMETER,
                )
            logger.debug(f"Moved parameter {param_fqn} to {callee_name}")  # noqa: G004

            # Next step is to replace placeholder of submodule with a get_attr.
            # Those placeholders are created by `split_module` inside each
            # submodule.
            # Update: this step is now moved to `_sink_params` because
            # `_sink_params` can do it recursively (i.e. for modules inside
            # submodule)

            to_delete.append((mod_itr, atoms[-1]))

        # Get the list of all parameters in the root module
        attr_nodes = list(filter(lambda n: n.op == "get_attr", split.graph.nodes))
        for node in attr_nodes:
            # Check whether the parameter is used in only one submodule
            if len(node.users) > 1:
                logger.info(
                    f"Parameter {node.target} used in multiple stages: {node.users}."  # noqa: G004
                )
            for user in node.users:
                assert user.op == "call_module"
                # Move parameter into submodule
                move_param_to_callee(
                    split,
                    user.target,
                    node.target,
                )

        # Deferral deletion: Remove the original attributes (to params) from the
        # root GraphModule
        for mod_itr, last_atom in to_delete:
            try:
                delattr(mod_itr, last_atom)
            except AttributeError:
                # This is expected if the parameter is used in multiple stages
                pass

        # After moving the params to their corresponding hierarchies, we also
        # need to move the `get_attr` nodes from the root of the graph to those
        # hierarchies.
        inputs_to_state: Dict[str, List[str]] = {
            attr.name: [attr.target] for attr in attr_nodes
        }
        # This is done by (1) `_sink_params` at each submodule;
        for name, submod in split.named_children():
            if isinstance(submod, fx.GraphModule):
                _sink_params(submod, inputs_to_state, [])
                submod.graph.lint()
                submod.recompile()

        for node in attr_nodes:
            # And (2): remove `get_attr` node from submod's arg list
            for user in copy.copy(node.users):
                assert user.op == "call_module"
                delete_user_reference(node, user)
            # And (3): remove the `get_attr` node from the root graph.
            split.graph.erase_node(node)

        split.delete_all_unused_submodules()
        split.graph.lint()
        split.recompile()

        num_stages = Pipe._number_and_count_forward_stages(split)

        has_loss_and_backward = False
        generated_loss_spec = output_loss_value_spec

        if output_loss_value_spec is not None:
            loss_node, output_node, generated_loss_spec = _find_loss_output(
                mod, split.graph, output_loss_value_spec
            )
            if loss_node is not None:
                _insert_stage_symbolic_backward(
                    split.graph,
                    loss_node,
                    output_node,
                )
                split.recompile()
                has_loss_and_backward = True
                logger.debug("Pipeline is in training mode, backward pass generated")
            else:
                raise RuntimeError(
                    f"Did not find any loss value according to {output_loss_value_spec=}"
                )
        else:
            logger.debug("Pipeline is in inference mode, backward pass not generated")

        logger.debug("Full pipe model:\n" f"{split}")  # noqa: G004

        return Pipe(
            split,
            num_stages,
            has_loss_and_backward,
            generated_loss_spec,
        )

    def print_readable(self):
        """
        Print the pipe in a human-readable format.
        This will print both the root pipe and each stage module.
        """
        self.split_gm.print_readable()

    @staticmethod
    def _trace_with_export(
        mod: torch.nn.Module,
        example_args: Tuple[Any, ...],
        example_kwargs: Optional[Dict[str, Any]] = None,
    ) -> ExportedProgram:
        logger.info("Tracing model ...")
        ep = torch.export.export(
            mod,
            example_args,
            example_kwargs,
        )
        return ep

    @staticmethod
    def from_tracing(
        mod: torch.nn.Module,
        num_chunks: int,
        example_args: Tuple[Any, ...],
        example_kwargs: Optional[Dict[str, Any]] = None,
        split_policy: Optional[Callable[[fx.GraphModule], fx.GraphModule]] = None,
    ):
        # If a param will be used in multiple pipeline stages, we default the strategy to REPLICATE'ing the param across
        # stages instead of TRANSMIT'ting it
        multi_use_param_spec = MultiUseParameterConfig.REPLICATE

        # Figure out which output is loss from output_chunk_spec
        output_loss_value_spec: Any = None
        # Deprecated
        """
        if output_chunk_spec is not None:
            output_loss_value_spec = map_aggregate(
                output_chunk_spec, lambda v: isinstance(v, _LossReducer)
            )
        """

        args_split, kwargs_split = split_args_kwargs_into_chunks(
            example_args,
            example_kwargs,
            num_chunks,
            Pipe.args_chunk_spec,
            Pipe.kwargs_chunk_spec,
        )

        # Trace with export
        exported_program = Pipe._trace_with_export(
            mod,
            example_args=args_split[0],
            example_kwargs=kwargs_split[0],
        )

        pipe = Pipe._from_traced(
            mod,
            exported_program,
            multi_use_param_spec,
            output_loss_value_spec=output_loss_value_spec,
            split_policy=split_policy,
        )

        # Users want the first pipeline stage to accept kwargs if the original
        # program does. This is controlled by the `_codegen` field of the graph,
        # so we make a copy here. Note: we only want the input spec and not the
        # output spec, because the output spec is for the last stage. Maybe a
        # TODO? Not sure yet.
        split = pipe.split_gm
        traced = exported_program.module()
        submod0 = next(iter(split.children()))
        submod0_sign = signature(submod0.forward)
        model_sign = signature(traced.forward)
        if len(model_sign.parameters) != len(submod0_sign.parameters):
            # We don't change the signature of the first stage if it takes
            # different number of args than original model
            logger.info(
                f"Original model takes {len(model_sign.parameters)} args but the "  # noqa: G004
                f"first pipeline stage takes {len(submod0_sign.parameters)}. "
                "Please provide args to respective pipeline stages."
            )
        else:
            # Support kwargs for the first stage
            submod0.graph._codegen = copy.deepcopy(traced.graph._codegen)
            # `_replace` is actually not "private" or internal. based on this doc:
            # To prevent conflicts with field names, the method and attribute names
            # start with an underscore
            submod0.graph._codegen.pytree_info = (
                submod0.graph._codegen.pytree_info._replace(out_spec=None)
            )
            submod0.recompile()

        # Create pipe info
        pipe.pipe_info = Pipe.PipeInfo(
            graph=pipe.split_gm.graph,
            num_stages=pipe.num_stages,
            num_chunks=num_chunks,
            has_loss_and_backward=pipe.has_loss_and_backward,
            args_chunk_spec=Pipe.args_chunk_spec,
            kwargs_chunk_spec=Pipe.kwargs_chunk_spec,
        )
        return pipe

    def __str__(self):
        return self.split_gm.__str__()

    def __repr__(self):
        return self.split_gm.__repr__()

    def info(self) -> PipeInfo:
        if self.pipe_info is None:
            raise RuntimeError(
                "Pipe info is not available. Please use the `pipeline` method to create the `Pipe` object."
            )
        return self.pipe_info


class SplitPoint(Enum):
    BEGINNING = 1
    END = 2


# For backward compatibility, we kept the PipeSplitWrapper class because `class
# SplitPoint` used to be defined in this class.
class PipeSplitWrapper:
    # Create a class alias for BC
    SplitPoint = SplitPoint


def _split_before_forward(self, *args, **kwargs):
    pipe_split()
    return self._orig_forward(*args, **kwargs)


def _split_after_forward(self, *args, **kwargs):
    try:
        return self._orig_forward(*args, **kwargs)
    finally:
        pipe_split()


def annotate_split_points(mod: torch.nn.Module, spec: Dict[str, SplitPoint]):
    # TODO: make this implementation out-of-place?
    for qualname, split_type in spec.items():
        atoms = qualname.split(".")
        predecessor_module = mod
        for i, atom in enumerate(atoms[:-1]):
            try:
                predecessor_module = getattr(predecessor_module, atom)
            except AttributeError as e:
                raise AttributeError(
                    f'Specified target {qualname} referenced nonexistent module {".".join(atoms[:i+1])}'
                ) from e

        mod_to_wrap = getattr(predecessor_module, atoms[-1])
        mod_to_wrap._orig_forward = mod_to_wrap.forward
        if split_type == SplitPoint.BEGINNING:
            mod_to_wrap.forward = MethodType(_split_before_forward, mod_to_wrap)
        elif split_type == SplitPoint.END:
            mod_to_wrap.forward = MethodType(_split_after_forward, mod_to_wrap)
        else:
            raise ValueError("Unknown split point type.")


def pipeline(
    module: torch.nn.Module,
    num_chunks: int,
    example_args: Tuple[Any, ...],
    example_kwargs: Optional[Dict[str, Any]] = None,
    split_spec: Optional[Dict[str, SplitPoint]] = None,
    split_policy: Optional[Callable[[fx.GraphModule], fx.GraphModule]] = None,
) -> Pipe:
    """
    Creates a pipeline representation for the provided module.

    See `Pipe` for more details.

    Arguments
    ---------
    module:
        The module to be transformed into a `Pipe`.
    num_chunks:
        The number of microbatches to be run with this pipeline.
    example_args:
        Example positional inputs to be used with this pipeline.
    example_kwargs:
        Example keyword inputs to be used with this pipeline. (default: `None`)
    split_spec:
        A dictionary mapping module names to `SplitPoint`s. (default: `None`)
    split_policy:
        The policy to use for splitting the module. (default: `None`)

    Returns
    -------
    A pipeline representation of class `Pipe`.
    """
    if split_spec is not None and split_policy is not None:
        raise ValueError(
            "Cannot specify both `split_spec` and `split_policy`. Please use only one of them."
        )

    if split_spec is not None:
        # Annotate split points in the module based on user spec
        annotate_split_points(module, split_spec)
        return Pipe.from_tracing(
            mod=module,
            num_chunks=num_chunks,
            example_args=example_args,
            example_kwargs=example_kwargs,
        )
    else:
        # Use split policy
        return Pipe.from_tracing(
            mod=module,
            num_chunks=num_chunks,
            example_args=example_args,
            example_kwargs=example_kwargs,
            split_policy=split_policy,
        )


class ArgsChunkSpec:
    """
    Context manager for setting `args_chunk_spec` during creation of Pipe

    Example:
        >>> # xdoctest: +SKIP
        >>> # There are three positional arguments to the model, and
        >>> # we are chunking them along dimension 0, 0 and 1, respectively
        >>> with ArgsChunkSpec((0, 0, 1)):
        >>>     pipe = pipeline(model, num_chunks, example_args)
    """

    def __init__(
        self,
        chunk_dims: Tuple[int, ...],
    ):
        self.args_chunk_spec = map_aggregate(
            chunk_dims,
            lambda dim: TensorChunkSpec(dim),
        )

    def __enter__(self):
        # Inject into the Pipe class
        Pipe.args_chunk_spec = self.args_chunk_spec
        return self.args_chunk_spec

    def __exit__(self, exc_type, exc_val, traceback):
        # Remove from the Pipe class
        Pipe.args_chunk_spec = None


class KwargsChunkSpec:
    """
    Context manager for setting `kwargs_chunk_spec` during creation of Pipe

    Example:
        >>> # xdoctest: +SKIP
        >>> # Chunk dimension 0 for the "id" argument, 1 for the "mask" argument
        >>> with KwargsChunkSpec({"id": 0, "mask": 1}):
        >>>     pipe = pipeline(model, num_chunks, (), example_kwargs)
    """

    def __init__(
        self,
        chunk_dims: Dict[str, int],
    ):
        self.kwargs_chunk_spec = map_aggregate(
            chunk_dims,
            lambda dim: TensorChunkSpec(dim),
        )

    def __enter__(self):
        # Inject into the Pipe class
        Pipe.kwargs_chunk_spec = self.kwargs_chunk_spec
        return self.kwargs_chunk_spec

    def __exit__(self, exc_type, exc_val, traceback):
        # Remove from the Pipe class
        Pipe.kwargs_chunk_spec = None
