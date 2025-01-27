"""
helper functions for edge stuff
"""

from collections import defaultdict
from typing import Dict
from typing import List
from typing import Tuple
from typing import Iterable
from typing import Callable
from typing import Optional

import numpy as np


from . import Edges
from . import EDGE_TYPES
from ..types import empty_2d
from ..utils import basetypes
from ..chunks import utils as chunk_utils
from ..meta import ChunkedGraphMeta
from ..connectivity.search import check_reachability
from ..utils.flatgraph import build_gt_graph
from ...utils.general import reverse_dictionary


def concatenate_chunk_edges(chunk_edge_dicts: List) -> Dict:
    """combine edge_dicts of multiple chunks into one edge_dict"""
    edges_dict = {}
    for edge_type in EDGE_TYPES:
        sv_ids1 = [np.array([], dtype=basetypes.NODE_ID)]
        sv_ids2 = [np.array([], dtype=basetypes.NODE_ID)]
        affinities = [np.array([], dtype=basetypes.EDGE_AFFINITY)]
        areas = [np.array([], dtype=basetypes.EDGE_AREA)]
        for edge_d in chunk_edge_dicts:
            edges = edge_d[edge_type]
            sv_ids1.append(edges.node_ids1)
            sv_ids2.append(edges.node_ids2)
            affinities.append(edges.affinities)
            areas.append(edges.areas)

        sv_ids1 = np.concatenate(sv_ids1)
        sv_ids2 = np.concatenate(sv_ids2)
        affinities = np.concatenate(affinities)
        areas = np.concatenate(areas)
        edges_dict[edge_type] = Edges(
            sv_ids1, sv_ids2, affinities=affinities, areas=areas
        )
    return edges_dict


def concatenate_cross_edge_dicts(cross_edge_dicts: Iterable) -> Dict:
    """Combines multiple cross edge dicts."""
    # print(cross_edge_dicts)
    result_d = {}
    for cross_edge_d in cross_edge_dicts:
        result_d = merge_cross_edge_dicts_single(result_d, cross_edge_d)
    return result_d


def merge_cross_edge_dicts_single(x_edges_d1: Dict, x_edges_d2: Dict) -> Dict:
    """Combines two cross chunk edge dicts of form {layer id : edge list}."""
    result_d = {}
    if not x_edges_d1 and not x_edges_d2:
        return result_d
    layers = np.unique(list(x_edges_d1.keys()) + list(x_edges_d2.keys()))
    for layer in range(2, max(layers) + 1):
        edges1 = x_edges_d1.get(layer, empty_2d)
        edges2 = x_edges_d2.get(layer, empty_2d)
        edges1 = np.array(edges1, dtype=basetypes.NODE_ID)
        edges2 = np.array(edges2, dtype=basetypes.NODE_ID)
        result_d[layer] = np.concatenate([edges1, edges2])
    return result_d


def merge_cross_edge_dicts_multiple(x_edges_d1: Dict, x_edges_d2: Dict) -> Dict:
    """
    Combines two cross chunk dictionaries of form
    {node_id: {layer id : edge list}}.
    """
    node_ids = np.unique(list(x_edges_d1.keys()) + list(x_edges_d2.keys()))
    result_d = {}
    for node_id in node_ids:
        result_d[node_id] = merge_cross_edge_dicts_single(
            x_edges_d1.get(node_id, {}), x_edges_d2.get(node_id, {})
        )
    return result_d


def categorize_edges(
    meta: ChunkedGraphMeta, supervoxels: np.ndarray, edges: Edges
) -> Tuple[Edges, Edges, Edges]:
    """
    Find edges and categorize them into:
    `in_edges`
        between given supervoxels
        (sv1, sv2) - sv1 in supervoxels and sv2 in supervoxels
    `out_edges`
        originating from given supervoxels but within chunk
        (sv1, sv2) - sv1 in supervoxels and sv2 not in supervoxels
    `cross_edges`
        originating from given supervoxels but crossing chunk boundary
    """
    from ..utils.context_managers import TimeIt

    mask1 = np.isin(edges.node_ids1, supervoxels)
    mask2 = np.isin(edges.node_ids2, supervoxels)
    in_mask = mask1 & mask2
    out_mask = mask1 & ~mask2

    print("np.sum(in_mask)", np.sum(in_mask))

    in_edges = edges[in_mask]
    all_out_edges = edges[out_mask]  # out_edges + cross_edges

    edge_layers = get_cross_chunk_edges_layer(meta, all_out_edges.get_pairs())
    cross_edges_mask = edge_layers > 1
    out_edges = all_out_edges[~cross_edges_mask]
    cross_edges = all_out_edges[cross_edges_mask]
    return (in_edges, out_edges, cross_edges)


def categorize_edges_v2(
    meta: ChunkedGraphMeta,
    supervoxels: np.ndarray,
    edges: Edges,
    l2id_children_d: Dict,
    get_sv_parents: Callable,
) -> Tuple[Edges, Edges, Edges]:
    """Faster version of categorize_edges(), avoids looping over L2 IDs."""
    node_ids1 = get_sv_parents(edges.node_ids1)
    node_ids2 = get_sv_parents(edges.node_ids2)

    layer_mask1 = chunk_utils.get_chunk_layers(meta, node_ids1) > 1
    in_edges = edges[node_ids1 == node_ids2]
    all_out_ = edges[layer_mask1 & (node_ids1 != node_ids2)]

    cx_layers = get_cross_chunk_edges_layer(meta, all_out_.get_pairs())
    cx_mask = cx_layers > 1
    out_edges = all_out_[~cx_mask]
    cross_edges = all_out_[cx_mask]
    return (in_edges, out_edges, cross_edges)


def get_cross_chunk_edges_layer(meta: ChunkedGraphMeta, cross_edges: Iterable):
    """Computes the layer in which a cross chunk edge becomes relevant.
    I.e. if a cross chunk edge links two nodes in layer 4 this function
    returns 3.
    :param cross_edges: n x 2 array
        edges between atomic (level 1) node ids
    :return: array of length n
    """
    if len(cross_edges) == 0:
        return np.array([], dtype=int)
    cross_chunk_edge_layers = np.ones(len(cross_edges), dtype=int)
    coords0 = chunk_utils.get_chunk_coordinates_multiple(meta, cross_edges[:, 0])
    coords1 = chunk_utils.get_chunk_coordinates_multiple(meta, cross_edges[:, 1])

    for _ in range(2, meta.layer_count):
        edge_diff = np.sum(np.abs(coords0 - coords1), axis=1)
        cross_chunk_edge_layers[edge_diff > 0] += 1
        coords0 = coords0 // meta.graph_config.FANOUT
        coords1 = coords1 // meta.graph_config.FANOUT
    return cross_chunk_edge_layers


def filter_min_layer_cross_edges(
    meta: ChunkedGraphMeta, cross_edges_d: Dict, node_layer: int = 2
) -> Tuple[int, Iterable]:
    """
    Given a dict of cross chunk edges {layer: edges}
    Return the first layer with cross edges.
    """
    for layer in range(node_layer, meta.layer_count):
        edges_ = cross_edges_d.get(layer, empty_2d)
        if edges_.size:
            return (layer, edges_)
    return (meta.layer_count, edges_)


def filter_min_layer_cross_edges_multiple(
    meta: ChunkedGraphMeta, l2id_atomic_cross_edges_ds: List, node_layer: int = 2
) -> Tuple[int, Iterable]:
    """
    Given a list of dicts of cross chunk edges [{layer: edges}]
    Return the first layer with cross edges.
    """
    min_layer = meta.layer_count
    for edges_d in l2id_atomic_cross_edges_ds:
        layer_, _ = filter_min_layer_cross_edges(meta, edges_d, node_layer=node_layer)
        min_layer = min(min_layer, layer_)
    edges = [empty_2d]
    for edges_d in l2id_atomic_cross_edges_ds:
        edges.append(edges_d.get(min_layer, empty_2d))
    return min_layer, np.concatenate(edges)


def get_edges_status(cg, edges: Iterable, time_stamp: Optional[float] = None):
    from ...utils.general import in2d

    coords0 = chunk_utils.get_chunk_coordinates_multiple(cg.meta, edges[:, 0])
    coords1 = chunk_utils.get_chunk_coordinates_multiple(cg.meta, edges[:, 1])

    coords = np.concatenate([np.array(coords0), np.array(coords1)])
    bbox = [np.min(coords, axis=0), np.max(coords, axis=0)]
    bbox[1] += 1

    root_ids = set(
        cg.get_roots(edges.ravel(), assert_roots=True, time_stamp=time_stamp)
    )
    sg_edges = cg.get_subgraph(
        root_ids,
        bbox=bbox,
        bbox_is_coordinate=False,
        edges_only=True,
    )
    existence_status = in2d(edges, sg_edges)
    edge_layers = cg.get_cross_chunk_edges_layer(edges)
    active_status = []
    for layer in np.unique(edge_layers):
        layer_edges = edges[edge_layers == layer]
        edges_parents = cg.get_roots(
            layer_edges.ravel(), time_stamp=time_stamp, stop_layer=layer + 1
        ).reshape(-1, 2)
        mask = edges_parents[:, 0] == edges_parents[:, 1]
        active_status.extend(mask)
    active_status = np.array(active_status, dtype=bool)
    return existence_status, active_status
