import collections
import numpy as np
import networkx as nx
import itertools
import logging
from networkx.algorithms.flow import shortest_augmenting_path, edmonds_karp, preflow_push
from networkx.algorithms.connectivity import minimum_st_edge_cut
import time
import graph_tool
import graph_tool.flow

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .utils import flatgraph
from . import chunkedgraph_exceptions as cg_exceptions

float_max = np.finfo(np.float32).max
DEBUG_MODE = False

def merge_cross_chunk_edges(edges: Iterable[Sequence[np.uint64]],
                            affs: Sequence[np.uint64],
                            logger: Optional[logging.Logger] = None):
    """ Merges cross chunk edges
    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :return:
    """
    # mask for edges that have to be merged
    cross_chunk_edge_mask = np.isinf(affs)

    # graph with edges that have to be merged
    cross_chunk_graph = nx.Graph()
    cross_chunk_graph.add_edges_from(edges[cross_chunk_edge_mask])

    # connected components in this graph will be combined in one component
    ccs = nx.connected_components(cross_chunk_graph)

    # Build mapping
    # For each connected component the smallest node id is chosen to be the
    # representative.
    remapping = {}
    mapping_ks = []
    mapping_vs = []

    for cc in ccs:
        nodes = np.array(list(cc))
        rep_node = np.min(nodes)

        remapping[rep_node] = nodes
        mapping_ks.extend(nodes)
        mapping_vs.extend([rep_node] * len(nodes))

    # Initialize mapping with a each node mapping to itself, then update
    # those edges merged to one across chunk boundaries.
    # u_nodes = np.unique(edges)
    # mapping = dict(zip(u_nodes, u_nodes))
    # mapping.update(dict(zip(mapping_ks, mapping_vs)))
    mapping = dict(zip(mapping_ks, mapping_vs))

    # Vectorize remapping
    mapping_vec = np.vectorize(lambda a : mapping[a] if a in mapping else a)
    remapped_edges = mapping_vec(edges)

    # Remove cross chunk edges
    remapped_edges = remapped_edges[~cross_chunk_edge_mask]
    remapped_affs = affs[~cross_chunk_edge_mask]

    return remapped_edges, remapped_affs, mapping, remapping


def merge_cross_chunk_edges_graph_tool(edges: Iterable[Sequence[np.uint64]],
                                       affs: Sequence[np.uint64],
                                       logger: Optional[logging.Logger] = None):
    """ Merges cross chunk edges
    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :return:
    """

    # mask for edges that have to be merged
    cross_chunk_edge_mask = np.isinf(affs)

    # graph with edges that have to be merged
    graph, _, _, unique_ids = flatgraph.build_gt_graph(
        edges[cross_chunk_edge_mask], make_directed=True)

    # connected components in this graph will be combined in one component
    ccs = flatgraph.connected_components(graph)

    remapping = {}
    mapping = np.array([], dtype=np.uint64).reshape(-1, 2)

    for cc in ccs:
        nodes = unique_ids[cc]
        rep_node = np.min(nodes)

        remapping[rep_node] = nodes

        rep_nodes = np.ones(len(nodes), dtype=np.uint64).reshape(-1, 1) * rep_node
        m = np.concatenate([nodes.reshape(-1, 1), rep_nodes], axis=1)

        mapping = np.concatenate([mapping, m], axis=0)

    u_nodes = np.unique(edges)
    u_unmapped_nodes = u_nodes[~np.in1d(u_nodes, mapping)]

    unmapped_mapping = np.concatenate([u_unmapped_nodes.reshape(-1, 1),
                                       u_unmapped_nodes.reshape(-1, 1)], axis=1)
    mapping = np.concatenate([mapping, unmapped_mapping], axis=0)

    sort_idx = np.argsort(mapping[:, 0])
    idx = np.searchsorted(mapping[:, 0], edges, sorter=sort_idx)
    remapped_edges = np.asarray(mapping[:, 1])[sort_idx][idx]

    remapped_edges = remapped_edges[~cross_chunk_edge_mask]
    remapped_affs = affs[~cross_chunk_edge_mask]

    return remapped_edges, remapped_affs, mapping, remapping


def mincut_nx(edges: Iterable[Sequence[np.uint64]], affs: Sequence[np.uint64],
              sources: Sequence[np.uint64], sinks: Sequence[np.uint64],
              logger: Optional[logging.Logger] = None) -> np.ndarray:
    """ Computes the min cut on a local graph
    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :param sources: uint64
    :param sinks: uint64
    :return: m x 2 array of uint64s
        edges that should be removed
    """

    time_start = time.time()

    original_edges = edges.copy()

    edges, affs, mapping, remapping = merge_cross_chunk_edges(edges.copy(),
                                                              affs.copy())
    mapping_vec = np.vectorize(lambda a: mapping[a] if a in mapping else a)

    if len(edges) == 0:
        return []

    if len(mapping) > 0:
        assert np.unique(list(mapping.keys()), return_counts=True)[1].max() == 1

    remapped_sinks = mapping_vec(sinks)
    remapped_sources = mapping_vec(sources)

    sinks = remapped_sinks
    sources = remapped_sources

    sink_connections = np.array(list(itertools.product(sinks, sinks)))
    source_connections = np.array(list(itertools.product(sources, sources)))

    weighted_graph = nx.Graph()
    weighted_graph.add_edges_from(edges)
    weighted_graph.add_edges_from(sink_connections)
    weighted_graph.add_edges_from(source_connections)

    for i_edge, edge in enumerate(edges):
        weighted_graph[edge[0]][edge[1]]['capacity'] = affs[i_edge]
        weighted_graph[edge[1]][edge[0]]['capacity'] = affs[i_edge]

    # Add infinity edges for multicut
    for sink_i in sinks:
        for sink_j in sinks:
            weighted_graph[sink_i][sink_j]['capacity'] = float_max

    for source_i in sources:
        for source_j in sources:
            weighted_graph[source_i][source_j]['capacity'] = float_max


    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Graph creation: %.2fms" % (dt * 1000))
    time_start = time.time()

    ccs = list(nx.connected_components(weighted_graph))

    for cc in ccs:
        cc_list = list(cc)

        # If connected component contains no sources and/or no sinks,
        # remove its nodes from the mincut computation
        if not np.any(np.in1d(sources, cc_list)) or \
                not np.any(np.in1d(sinks, cc_list)):
            weighted_graph.remove_nodes_from(cc)

    r_flow = edmonds_karp(weighted_graph, sinks[0], sources[0])
    cutset = minimum_st_edge_cut(weighted_graph, sources[0], sinks[0],
                                 residual=r_flow)

    # cutset = nx.minimum_edge_cut(weighted_graph, sources[0], sinks[0], flow_func=edmonds_karp)

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Mincut comp: %.2fms" % (dt * 1000))

    if cutset is None:
        return []

    time_start = time.time()

    edge_cut = list(list(cutset))

    weighted_graph.remove_edges_from(edge_cut)
    ccs = list(nx.connected_components(weighted_graph))

    # assert len(ccs) == 2

    for cc in ccs:
        cc_list = list(cc)
        if logger is not None:
            logger.debug("CC size = %d" % len(cc_list))

        if np.any(np.in1d(sources, cc_list)):
            assert np.all(np.in1d(sources, cc_list))
            assert ~np.any(np.in1d(sinks, cc_list))

        if np.any(np.in1d(sinks, cc_list)):
            assert np.all(np.in1d(sinks, cc_list))
            assert ~np.any(np.in1d(sources, cc_list))

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Splitting local graph: %.2fms" % (dt * 1000))

    remapped_cutset = []
    for cut in cutset:
        if cut[0] in remapping:
            pre_cut = remapping[cut[0]]
        else:
            pre_cut = [cut[0]]

        if cut[1] in remapping:
            post_cut = remapping[cut[1]]
        else:
            post_cut = [cut[1]]

        remapped_cutset.extend(list(itertools.product(pre_cut, post_cut)))
        remapped_cutset.extend(list(itertools.product(post_cut, pre_cut)))

    remapped_cutset = np.array(remapped_cutset, dtype=np.uint64)

    remapped_cutset_flattened_view = remapped_cutset.view(dtype='u8,u8')
    edges_flattened_view = original_edges.view(dtype='u8,u8')

    cutset_mask = np.in1d(remapped_cutset_flattened_view, edges_flattened_view)

    return remapped_cutset[cutset_mask]


# TODO: Refactor/break up this long function into several functions
def mincut_graph_tool(edges: Iterable[Sequence[np.uint64]],
                      affs: Sequence[np.uint64],
                      sources: Sequence[np.uint64],
                      sinks: Sequence[np.uint64],
                      logger: Optional[logging.Logger] = None) -> np.ndarray:
    """ Computes the min cut on a local graph
    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :param sources: uint64
    :param sinks: uint64
    :return: m x 2 array of uint64s
        edges that should be removed
    """
    time_start = time.time()

    original_edges = edges

    # Stitch supervoxels across chunk boundaries and represent those that are
    # connected with a cross chunk edge with a single id. This may cause id
    # changes among sinks and sources that need to be taken care of.
    edges, affs, mapping, remapping = merge_cross_chunk_edges(edges.copy(),
                                                              affs.copy())

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Cross edge merging: %.2fms" % (dt * 1000))
    time_start = time.time()

    mapping_vec = np.vectorize(lambda a: mapping[a] if a in mapping else a)

    if len(edges) == 0:
        return []

    if len(mapping) > 0:
        assert np.unique(list(mapping.keys()), return_counts=True)[1].max() == 1

    remapped_sinks = mapping_vec(sinks)
    remapped_sources = mapping_vec(sources)

    sinks = remapped_sinks
    sources = remapped_sources

    # Assemble edges: Edges after remapping combined with edges between sinks
    # and sources
    sink_edges = list(itertools.product(sinks, sinks))
    source_edges = list(itertools.product(sources, sources))

    comb_edges = np.concatenate([edges, sink_edges, source_edges])

    comb_affs = np.concatenate([affs, [float_max, ] *
                                (len(sink_edges) + len(source_edges))])

    # To make things easier for everyone involved, we map the ids to
    # [0, ..., len(unique_ids) - 1]
    # Generate weighted graph with graph_tool
    weighted_graph, cap, gt_edges, unique_ids = \
        flatgraph.build_gt_graph(comb_edges, comb_affs,
                                       make_directed=True)

    # Create an edge property to remove edges later (will be used to test whether split valid)
    is_fake_edge = np.concatenate(
        [[False] * len(affs), [True] * (len(sink_edges) + len(source_edges))]
    )
    remove_edges_later = np.concatenate([is_fake_edge, is_fake_edge])
    edges_to_remove = weighted_graph.new_edge_property("bool", vals=remove_edges_later)

    sink_graph_ids = np.where(np.in1d(unique_ids, sinks))[0]
    source_graph_ids = np.where(np.in1d(unique_ids, sources))[0]

    if logger is not None:
        logger.debug(f"{sinks}, {sink_graph_ids}")
        logger.debug(f"{sources}, {source_graph_ids}")

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Graph creation: %.2fms" % (dt * 1000))
    time_start = time.time()

    # Get rid of connected components that are not involved in the local
    # mincut
    ccs = flatgraph.connected_components(weighted_graph)

    removed = weighted_graph.new_vertex_property("bool")
    removed.a = False
    if len(ccs) > 1:
        for cc in ccs:
            # If connected component contains no sources or no sinks,
            # remove its nodes from the mincut computation
            if not (np.any(np.in1d(source_graph_ids, cc)) and \
                    np.any(np.in1d(sink_graph_ids, cc))):
                for node_id in cc:
                    removed[node_id] = True

    weighted_graph.set_vertex_filter(removed, inverted=True)

    # Somewhat untuitively, we need to create a new pruned graph for the following
    # connected components call to work correctly, because the vertex filter
    # only labels the graph and the filtered vertices still show up after running
    # graph_tool.label_components
    pruned_graph = graph_tool.Graph(weighted_graph, prune=True)

    # Test that there is only one connected component left
    ccs = flatgraph.connected_components(pruned_graph)

    if len(ccs) > 1:
        logger.warning("Not all sinks and sources are within the same (local)"
                       "connected component")
        raise cg_exceptions.PreconditionError(
                "Not all sinks and sources are within the same (local)"
                "connected component"
            )
    elif len(ccs) == 0:
        raise cg_exceptions.PreconditionError(
                "Sinks and sources are not connected through the local graph. "
                "Please try a different set of vertices to perform the mincut."
            )

    # Compute mincut
    src, tgt = weighted_graph.vertex(source_graph_ids[0]), \
               weighted_graph.vertex(sink_graph_ids[0])

    res = graph_tool.flow.push_relabel_max_flow(weighted_graph, src, tgt, cap)

    part = graph_tool.flow.min_st_cut(weighted_graph, src, cap, res)

    labeled_edges = part.a[gt_edges]
    cut_edge_set = gt_edges[labeled_edges[:, 0] != labeled_edges[:, 1]]

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Mincut comp: %.2fms" % (dt * 1000))
    time_start = time.time()

    if len(cut_edge_set) == 0:
        return []

    time_start = time.time()

    if DEBUG_MODE:
        # These assertions should not fail. If they do, 
        # then something went wrong with the graph_tool mincut computation
        for i_cc in np.unique(part.a):
            # Make sure to read real ids and not graph ids
            cc_list = unique_ids[np.array(np.where(part.a == i_cc)[0],
                                        dtype=np.int)]

            if np.any(np.in1d(sources, cc_list)):
                assert np.all(np.in1d(sources, cc_list))
                assert ~np.any(np.in1d(sinks, cc_list))

            if np.any(np.in1d(sinks, cc_list)):
                assert np.all(np.in1d(sinks, cc_list))
                assert ~np.any(np.in1d(sources, cc_list))

    weighted_graph.clear_filters()

    for cut_edge in cut_edge_set:
        # May be more than one edge from vertex cut_edge[0] to vertex cut_edge[1], add them all
        parallel_edges = weighted_graph.edge(cut_edge[0], cut_edge[1], all_edges=True)
        for edge_to_remove in parallel_edges:
            edges_to_remove[edge_to_remove] = True

    weighted_graph.set_filters(edges_to_remove, removed, True, True)

    ccs_test_post_cut = flatgraph.connected_components(weighted_graph)

    # Make sure sinks and sources are among each other and not in different sets
    # after removing the cut edges and the fake infinity edges
    try:
        for cc in ccs_test_post_cut:
            if np.any(np.in1d(source_graph_ids, cc)):
                assert np.all(np.in1d(source_graph_ids, cc))
                assert ~np.any(np.in1d(sink_graph_ids, cc))

            if np.any(np.in1d(sink_graph_ids, cc)):
                assert np.all(np.in1d(sink_graph_ids, cc))
                assert ~np.any(np.in1d(source_graph_ids, cc))
    except AssertionError:
        raise cg_exceptions.PreconditionError(
                "Failed to find a cut that separated the sources from the sinks. "
                "Please try another cut that partitions the sets cleanly if possible. "
                "If there is a clear path between all the supervoxels in each set, "
                "that helps the mincut algorithm."
            )

    dt = time.time() - time_start
    if logger is not None:
        logger.debug("Verifying local graph: %.2fms" % (dt * 1000))

    # Extract original ids
    # This has potential to be optimized
    remapped_cutset = []
    for s, t in flatgraph.remap_ids_from_graph(cut_edge_set, unique_ids):

        if s in remapping:
            s = remapping[s]
        else:
            s = [s]

        if t in remapping:
            t = remapping[t]
        else:
            t = [t]

        remapped_cutset.extend(list(itertools.product(s, t)))
        remapped_cutset.extend(list(itertools.product(t, s)))

    remapped_cutset = np.array(remapped_cutset, dtype=np.uint64)

    remapped_cutset_flattened_view = remapped_cutset.view(dtype='u8,u8')
    edges_flattened_view = original_edges.view(dtype='u8,u8')

    cutset_mask = np.in1d(remapped_cutset_flattened_view, edges_flattened_view)

    return remapped_cutset[cutset_mask]


def mincut(edges: Iterable[Sequence[np.uint64]],
           affs: Sequence[np.uint64],
           sources: Sequence[np.uint64],
           sinks: Sequence[np.uint64],
           logger: Optional[logging.Logger] = None) -> np.ndarray:
    """ Computes the min cut on a local graph
    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :param sources: uint64
    :param sinks: uint64
    :return: m x 2 array of uint64s
        edges that should be removed
    """

    return mincut_graph_tool(edges=edges, affs=affs, sources=sources,
                             sinks=sinks, logger=logger)
