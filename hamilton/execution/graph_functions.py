import logging
from typing import Any, Collection, Dict, List, Optional, Tuple

from hamilton import base, node

logger = logging.getLogger(__name__)

"""A set of utility functions for managing/traversing DAGs. Note these all operate on nodes.
We will likely want to genericize them so we're dealing with anything, not just node.Nodes.
"""


def topologically_sort_nodes(nodes: List[node.Node]) -> List[node.Node]:
    """Topologically sorts a list of nodes based on their dependencies.

    TODO -- use python graphlib when we no longer have to support 3.7/3.8.

    https://docs.python.org/3/library/graphlib.html

    :param nodes: Nodes to sort
    :return: Nodes in sorted order
    """

    in_degrees = {node_.name: len(node_.dependencies) for node_ in nodes}
    # TODO -- determine what happens if nodes have dependencies that aren't present
    sources = [node_ for node_ in nodes if len(node_.dependencies) == 0]
    queue = []
    for source in sources:
        queue.append(source)

    sorted_nodes = []
    while len(queue) > 0:
        node_ = queue.pop(0)
        sorted_nodes.append(node_)
        for next_node in node_.depended_on_by:
            if next_node.name in in_degrees:
                in_degrees[next_node.name] -= 1
                if in_degrees[next_node.name] == 0:
                    queue.append(next_node)

    return sorted_nodes


def get_node_levels(topologically_sorted_nodes: List[node.Node]) -> Dict[str, int]:
    """Gets the levels for a group of topologically sorted nodes.
    This only works if its topologically sorted, of course...


    :param topologically_sorted_nodes:
    :return: A dictionary of node name -> level
    """
    node_levels = {}
    node_set = {node_.name for node_ in topologically_sorted_nodes}
    for node_ in topologically_sorted_nodes:
        dependencies_in_set = {n.name for n in node_.dependencies}.intersection(node_set)
        if len(dependencies_in_set) == 0:
            node_levels[node_.name] = 0
        else:
            node_levels[node_.name] = max([node_levels[n] for n in dependencies_in_set]) + 1
    return node_levels


def combine_config_and_inputs(config: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Validates and combines config and inputs, ensuring that they're mutually disjoint.
    :param config: Config to construct, run the DAG with.
    :param inputs: Inputs to run the DAG on at runtime
    :return: The combined set of inputs to the DAG.
    :raises ValueError: if they are not disjoint
    """
    duplicated_inputs = [key for key in inputs if key in config]
    if len(duplicated_inputs) > 0:
        raise ValueError(
            f"The following inputs are present in both config and inputs. They must be "
            f"mutually disjoint. {duplicated_inputs} "
        )
    return {**config, **inputs}


def execute_subdag(
    nodes: Collection[node.Node],
    inputs: Dict[str, Any],
    adapter: base.HamiltonGraphAdapter,
    computed: Dict[str, Any] = None,
    overrides: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Base function to execute a subdag. This conducts a depth first traversal of the graph.

    :param nodes: Nodes to compute
    :param inputs: Inputs, external
    :param adapter:  Adapter to use to compute
    :param computed:  Already computed nodes
    :param overrides: Overrides to use, will short-circuit computation
    :return: The results
    """
    if overrides is None:
        overrides = {}
    if computed is None:
        computed = {}

    def dfs_traverse(
        node_: node.Node, dependency_type: node.DependencyType = node.DependencyType.REQUIRED
    ):
        if node_.name in computed:
            return
        if node_.name in overrides:
            computed[node_.name] = overrides[node_.name]
            return
        for n in node_.dependencies:
            if n.name not in computed:
                _, node_dependency_type = node_.input_types[n.name]
                dfs_traverse(n, node_dependency_type)

        logger.debug(f"Computing {node_.name}.")
        if node_.user_defined:
            if node_.name not in inputs:
                if dependency_type != node.DependencyType.OPTIONAL:
                    raise NotImplementedError(
                        f"{node_.name} was expected to be passed in but was not."
                    )
                return
            value = inputs[node_.name]
        else:
            kwargs = {}  # construct signature
            for dependency in node_.dependencies:
                if dependency.name in computed:
                    kwargs[dependency.name] = computed[dependency.name]
            try:
                value = adapter.execute_node(node_, kwargs)
            except Exception:
                logger.exception(f"Node {node_.name} encountered an error")
                raise
        computed[node_.name] = value

    for final_var_node in nodes:
        dep_type = node.DependencyType.REQUIRED
        if final_var_node.user_defined:
            # from the top level, we don't know if this UserInput is required. So mark as optional.
            dep_type = node.DependencyType.OPTIONAL
        dfs_traverse(final_var_node, dep_type)
    return computed


def nodes_between(
    end_node: node.Node,
    search_condition: lambda node_: bool,
) -> Tuple[Optional[node.Node], List[node.Node]]:
    """Utility function to search backwards from an end node to a start node.
    This returns all nodes for which the following conditions are met:

    1. It contains a node that matches the start_condition as an ancestor
    2. It contains a node that matches the end node as a dependent

    Note that currently it is assumed that only one node will
    match search_condition.

    :param end_node: Node to trace back from
    :param search_condition: Condition to stop the search for ancestors
    :return: A tuple of [start_node, between], where start_node is None
    if there is no path (and between will be empty).
    """

    visited = set()

    def dfs_traverse(node_: node.Node):
        """Recursive call. Note that it returns None to signify
        that we should not traverse any nodes, and a list to say that
        we should continue traversing"""
        if node_ in visited:
            return []
        visited.add(node_)
        if search_condition(node_):
            return [node_]
        if node_.user_defined:
            return None
        out = []
        for n in node_.dependencies:
            traverse = dfs_traverse(n)
            if traverse is not None:
                out.extend(traverse)
                out.append(n)
        if len(out) == 0:
            return None
        return out

    output = []
    for node_ in dfs_traverse(end_node) or []:
        output.append(node_)
    begin_node = None
    nodes = []
    for node_ in output:
        # TODO -- handle the case that there are multiple nodes that match the search condition
        if search_condition(node_):
            begin_node = node_
        elif node_ == end_node:
            continue
        else:
            nodes.append(node_)
    return begin_node, nodes