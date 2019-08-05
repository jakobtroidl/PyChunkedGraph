import collections
import time
import random

import pandas as pd
import cloudvolume
import networkx as nx
import numpy as np
import numpy.lib.recfunctions as rfn
import zstandard as zstd
from multiwrapper import multiprocessing_utils as mu

from pychunkedgraph.ingest import ingestionmanager, ingestion_utils as iu
from ..backend.chunkedgraph_init import add_atomic_edges_in_chunks
from ..backend.utils.edges import TYPES as EDGE_TYPES, Edges
from ..backend.utils import basetypes


def ingest_into_chunkedgraph(storage_path, ws_cv_path, cg_table_id,
                             chunk_size=[256, 256, 512],
                             use_skip_connections=True,
                             s_bits_atomic_layer=None,
                             fan_out=2, aff_dtype=np.float32,
                             size=None,
                             instance_id=None, project_id=None,
                             start_layer=1, n_threads=[64, 64]):
    """ Creates a chunkedgraph from a Ran Agglomerattion

    :param storage_path: str
        Google cloud bucket path (agglomeration)
        example: gs://ranl-scratch/minnie_test_2
    :param ws_cv_path: str
        Google cloud bucket path (watershed segmentation)
        example: gs://microns-seunglab/minnie_v0/minnie10/ws_minnie_test_2/agg
    :param cg_table_id: str
        chunkedgraph table name
    :param fan_out: int
        fan out of chunked graph (2 == Octree)
    :param aff_dtype: np.dtype
        affinity datatype (np.float32 or np.float64)
    :param instance_id: str
        Google instance id
    :param project_id: str
        Google project id
    :param start_layer: int
    :param n_threads: list of ints
        number of threads to use
    :return:
    """
    storage_path = storage_path.strip("/")
    ws_cv_path = ws_cv_path.strip("/")

    cg_mesh_dir = f"{cg_table_id}_meshes"
    chunk_size = np.array(chunk_size, dtype=np.uint64)

    _, n_layers_agg = iu.initialize_chunkedgraph(cg_table_id=cg_table_id,
                                                 ws_cv_path=ws_cv_path,
                                                 chunk_size=chunk_size,
                                                 size=size,
                                                 use_skip_connections=use_skip_connections,
                                                 s_bits_atomic_layer=s_bits_atomic_layer,
                                                 cg_mesh_dir=cg_mesh_dir,
                                                 fan_out=fan_out,
                                                 instance_id=instance_id,
                                                 project_id=project_id)

    im = ingestionmanager.IngestionManager(storage_path=storage_path,
                                           cg_table_id=cg_table_id,
                                           n_layers=n_layers_agg,
                                           instance_id=instance_id,
                                           project_id=project_id)

    # #TODO: Remove later:
    # logging.basicConfig(level=logging.DEBUG)
    # im.cg.logger = logging.getLogger(__name__)
    # ------------------------------------------
    if start_layer < 3:
        create_atomic_chunks(im, aff_dtype=aff_dtype, n_threads=n_threads[0])

    create_abstract_layers(im, n_threads=n_threads[1], start_layer=start_layer)

    return im


def create_abstract_layers(im, start_layer=3, n_threads=1):
    """ Creates abstract of chunkedgraph (> 2)

    :param im: IngestionManager
    :param n_threads: int
        number of threads to use
    :return:
    """
    if start_layer < 3:
        start_layer = 3

    assert start_layer < int(im.cg.n_layers + 1)

    for layer_id in range(start_layer, int(im.cg.n_layers + 1)):
        create_layer(im, layer_id, n_threads=n_threads)


def create_layer(im, layer_id, block_size=100, n_threads=1):
    """ Creates abstract layer of chunkedgraph

    Abstract layers have to be build in sequence. Abstract layers are all layers
    above the first layer (1). `create_atomic_chunks` creates layer 2 as well.
    Hence, this function is responsible for every creating layers > 2.

    :param im: IngestionManager
    :param layer_id: int
        > 2
    :param n_threads: int
        number of threads to use
    :return:
    """
    assert layer_id > 2

    child_chunk_coords = im.chunk_coords // im.cg.fan_out ** (layer_id - 3)
    child_chunk_coords = child_chunk_coords.astype(np.int)
    child_chunk_coords = np.unique(child_chunk_coords, axis=0)

    parent_chunk_coords = child_chunk_coords // im.cg.fan_out
    parent_chunk_coords = parent_chunk_coords.astype(np.int)
    parent_chunk_coords, inds = np.unique(parent_chunk_coords, axis=0,
                                          return_inverse=True)

    im_info = im.get_serialized_info()
    multi_args = []

    # Randomize chunks
    order = np.arange(len(parent_chunk_coords), dtype=np.int)
    np.random.shuffle(order)

    # Block chunks
    block_size = min(block_size, int(np.ceil(len(order) / n_threads / 3)))
    n_blocks = int(len(order) / block_size)
    blocks = np.array_split(order, n_blocks)

    for i_block, block in enumerate(blocks):
        chunks = []
        for idx in block:
            chunks.append(child_chunk_coords[inds == idx])

        multi_args.append([im_info, layer_id, len(order), n_blocks, i_block,
                           chunks])

    if n_threads == 1:
        mu.multiprocess_func(
            _create_layers, multi_args, n_threads=n_threads,
            verbose=True, debug=n_threads == 1)
    else:
        mu.multisubprocess_func(_create_layers, multi_args, n_threads=n_threads,
                                suffix=f"{layer_id}")


def _create_layers(args):
    """ Multiprocessing helper for create_layer """
    im_info, layer_id, n_chunks, n_blocks, i_block, chunks = args
    im = ingestionmanager.IngestionManager(**im_info)

    for i_chunk, child_chunk_coords in enumerate(chunks):
        time_start = time.time()

        im.cg.add_layer(layer_id, child_chunk_coords, n_threads=8, verbose=True)

        print(f"Layer {layer_id} - Job {i_block + 1} / {n_blocks} - "
              f"{i_chunk + 1} / {len(chunks)} -- %.3fs" %
              (time.time() - time_start))


def create_atomic_chunks(im, aff_dtype=np.float32, n_threads=1, block_size=100):
    """ Creates all atomic chunks

    :param im: IngestionManager
    :param aff_dtype: np.dtype
        affinity datatype (np.float32 or np.float64)
    :param n_threads: int
        number of threads to use
    :return:
    """

    im_info = im.get_serialized_info()

    multi_args = []

    # Randomize chunk order
    chunk_coords = list(im.chunk_coord_gen)
    order = np.arange(len(chunk_coords), dtype=np.int)
    np.random.shuffle(order)

    # Block chunks
    block_size = min(block_size, int(np.ceil(len(chunk_coords) / n_threads / 3)))
    n_blocks = int(len(chunk_coords) / block_size)
    blocks = np.array_split(order, n_blocks)

    for i_block, block in enumerate(blocks):
        chunks = []
        for b_idx in block:
            chunks.append(chunk_coords[b_idx])

        multi_args.append([im_info, aff_dtype, n_blocks, i_block, chunks])

    if n_threads == 1:
        mu.multiprocess_func(
            _create_atomic_chunk, multi_args, n_threads=n_threads,
            verbose=True, debug=n_threads == 1)
    else:
        mu.multisubprocess_func(
            _create_atomic_chunk, multi_args, n_threads=n_threads)


def _create_atomic_chunk(args):
    """ Multiprocessing helper for create_atomic_chunks """
    im_info, aff_dtype, n_blocks, i_block, chunks = args
    im = ingestionmanager.IngestionManager(**im_info)

    for i_chunk, chunk_coord in enumerate(chunks):
        time_start = time.time()

        create_atomic_chunk(im, chunk_coord, aff_dtype=aff_dtype, verbose=True)

        print(f"Layer 1 - {chunk_coord} - Job {i_block + 1} / {n_blocks} - "
              f"{i_chunk + 1} / {len(chunks)} -- %.3fs" %
              (time.time() - time_start))


def create_atomic_chunk(imanager, chunk_coord, aff_dtype=basetypes.EDGE_AFFINITY):
    """ Creates single atomic chunk

    :param imanager: IngestionManager
    :param chunk_coord: np.ndarray
        array of three ints
    :param aff_dtype: np.dtype
        np.float64 or np.float32
    :return:
    """
    chunk_coord = np.array(list(chunk_coord), dtype=np.int)

    edge_dict = collect_edge_data(imanager, chunk_coord, aff_dtype=aff_dtype)
    mapping = collect_agglomeration_data(imanager, chunk_coord)
    _, isolated_ids = define_active_edges(edge_dict, mapping)

    chunk_edges = {}
    for edge_type in EDGE_TYPES:
        supervoxel_ids1 = edge_dict[edge_type]["sv1"]
        supervoxel_ids2 = edge_dict[edge_type]["sv2"]

        ones = np.ones(len(supervoxel_ids1))
        affinities = edge_dict[edge_type].get("aff", float("inf") * ones)
        areas = edge_dict[edge_type].get("area", ones)
        chunk_edges[edge_type] = Edges(
            supervoxel_ids1, supervoxel_ids2, affinities, areas
        )

    add_atomic_edges_in_chunks(imanager.cg, chunk_coord, chunk_edges, isolated_node_ids=isolated_ids)

    # to track workers completion
    return chunk_coord


def _get_cont_chunk_coords(im, chunk_coord_a, chunk_coord_b):
    """ Computes chunk coordinates that compute data between the named chunks

    :param im: IngestionManagaer
    :param chunk_coord_a: np.ndarray
        array of three ints
    :param chunk_coord_b: np.ndarray
        array of three ints
    :return: np.ndarray
    """

    diff = chunk_coord_a - chunk_coord_b

    dir_dim = np.where(diff != 0)[0]
    assert len(dir_dim) == 1
    dir_dim = dir_dim[0]

    if diff[dir_dim] > 0:
        chunk_coord_l = chunk_coord_a
    else:
        chunk_coord_l = chunk_coord_b

    c_chunk_coords = []
    for dx in [-1, 0]:
        for dy in [-1, 0]:
            for dz in [-1, 0]:
                if dz == dy == dx == 0:
                    continue

                c_chunk_coord = chunk_coord_l + np.array([dx, dy, dz])

                if [dx, dy, dz][dir_dim] == 0:
                    continue

                if im.is_out_of_bounce(c_chunk_coord):
                    continue

                c_chunk_coords.append(c_chunk_coord)

    return c_chunk_coords


def collect_edge_data(im, chunk_coord, aff_dtype=np.float32):
    """ Loads edge for single chunk

    :param im: IngestionManager
    :param chunk_coord: np.ndarray
        array of three ints
    :param aff_dtype: np.dtype
    :return: dict of np.ndarrays
    """
    subfolder = "chunked_rg"

    base_path = f"{im.storage_path}/{subfolder}/"

    chunk_coord = np.array(chunk_coord)

    chunk_id = im.cg.get_chunk_id(layer=1, x=chunk_coord[0], y=chunk_coord[1],
                                  z=chunk_coord[2])

    filenames = collections.defaultdict(list)
    swap = collections.defaultdict(list)
    for x in [chunk_coord[0] - 1, chunk_coord[0]]:
        for y in [chunk_coord[1] - 1, chunk_coord[1]]:
            for z in [chunk_coord[2] - 1, chunk_coord[2]]:

                if im.is_out_of_bounce(np.array([x, y, z])):
                    continue

                # EDGES WITHIN CHUNKS
                filename = f"in_chunk_0_{x}_{y}_{z}_{chunk_id}.data"
                filenames["in"].append(filename)

    for d in [-1, 1]:
        for dim in range(3):
            diff = np.zeros([3], dtype=np.int)
            diff[dim] = d

            adjacent_chunk_coord = chunk_coord + diff
            adjacent_chunk_id = im.cg.get_chunk_id(layer=1,
                                                   x=adjacent_chunk_coord[0],
                                                   y=adjacent_chunk_coord[1],
                                                   z=adjacent_chunk_coord[2])

            if im.is_out_of_bounce(adjacent_chunk_coord):
                continue

            c_chunk_coords = _get_cont_chunk_coords(im, chunk_coord,
                                                    adjacent_chunk_coord)

            larger_id = np.max([chunk_id, adjacent_chunk_id])
            smaller_id = np.min([chunk_id, adjacent_chunk_id])
            chunk_id_string = f"{smaller_id}_{larger_id}"

            for c_chunk_coord in c_chunk_coords:
                x, y, z = c_chunk_coord

                # EDGES BETWEEN CHUNKS
                filename = f"between_chunks_0_{x}_{y}_{z}_{chunk_id_string}.data"
                filenames["between"].append(filename)

                swap[filename] = larger_id == chunk_id

                # EDGES FROM CUTS OF SVS
                filename = f"fake_0_{x}_{y}_{z}_{chunk_id_string}.data"
                filenames["cross"].append(filename)

                swap[filename] = larger_id == chunk_id

    edge_data = {}
    read_counter = collections.Counter()

    dtype = [("sv1", np.uint64), ("sv2", np.uint64),
             ("aff", aff_dtype), ("area", np.uint64)]
    for k in filenames:
        # print(k, len(filenames[k]))

        with cloudvolume.Storage(base_path, n_threads=10) as stor:
            files = stor.get_files(filenames[k])

        data = []
        for file in files:
            if file["content"] is None:
                # print(f"{file['filename']} not created or empty")
                continue

            if file["error"] is not None:
                # print(f"error reading {file['filename']}")
                continue

            if swap[file["filename"]]:
                this_dtype = [dtype[1], dtype[0], dtype[2], dtype[3]]
                content = np.frombuffer(file["content"], dtype=this_dtype)
            else:
                content = np.frombuffer(file["content"], dtype=dtype)

            data.append(content)

            read_counter[k] += 1

        try:
            edge_data[k] = rfn.stack_arrays(data, usemask=False)
        except:
            raise()

        edge_data_df = pd.DataFrame(edge_data[k])
        edge_data_dfg = edge_data_df.groupby(["sv1", "sv2"]).aggregate(np.sum).reset_index()
        edge_data[k] = edge_data_dfg.to_records()

    # # TEST
    # with cloudvolume.Storage(base_path, n_threads=10) as stor:
    #     files = list(stor.list_files())
    #
    # true_counter = collections.Counter()
    # for file in files:
    #     if str(chunk_id) in file:
    #         true_counter[file.split("_")[0]] += 1
    #
    # print("Truth", true_counter)
    # print("Reality", read_counter)

    return edge_data


def _read_agg_files(filenames, base_path):
    with cloudvolume.Storage(base_path, n_threads=10) as stor:
        files = stor.get_files(filenames)

    edge_list = []
    for file in files:
        if file["content"] is None:
            continue

        if file["error"] is not None:
            continue

        content = zstd.ZstdDecompressor().decompressobj().decompress(file["content"])
        edge_list.append(np.frombuffer(content, dtype=np.uint64).reshape(-1, 2))

    return edge_list


def collect_agglomeration_data(im, chunk_coord):
    """ Collects agglomeration information & builds connected component mapping

    :param im: IngestionManager
    :param chunk_coord: np.ndarray
        array of three ints
    :return: dictionary
    """
    subfolder = "remap"
    base_path = f"{im.storage_path}/{subfolder}/"

    chunk_coord = np.array(chunk_coord)

    chunk_id = im.cg.get_chunk_id(layer=1, x=chunk_coord[0], y=chunk_coord[1],
                                  z=chunk_coord[2])

    filenames = []
    for mip_level in range(0, int(im.n_layers - 1)):
        x, y, z = np.array(chunk_coord / 2 ** mip_level, dtype=np.int)
        filenames.append(f"done_{mip_level}_{x}_{y}_{z}_{chunk_id}.data.zst")

    for d in [-1, 1]:
        for dim in range(3):
            diff = np.zeros([3], dtype=np.int)
            diff[dim] = d

            adjacent_chunk_coord = chunk_coord + diff

            adjacent_chunk_id = im.cg.get_chunk_id(layer=1,
                                                   x=adjacent_chunk_coord[0],
                                                   y=adjacent_chunk_coord[1],
                                                   z=adjacent_chunk_coord[2])

            for mip_level in range(0, int(im.n_layers - 1)):
                x, y, z = np.array(adjacent_chunk_coord / 2 ** mip_level, dtype=np.int)
                filenames.append(f"done_{mip_level}_{x}_{y}_{z}_{adjacent_chunk_id}.data.zst")

    # print(filenames)
    edge_list = _read_agg_files(filenames, base_path)

    edges = np.concatenate(edge_list)

    G = nx.Graph()
    G.add_edges_from(edges)
    ccs = nx.connected_components(G)

    mapping = {}
    for i_cc, cc in enumerate(ccs):
        cc = list(cc)
        mapping.update(dict(zip(cc, [i_cc] * len(cc))))

    return mapping


def define_active_edges(edge_dict, mapping):
    """ Labels edges as within or across segments and extracts isolated ids

    :param edge_dict: dict of np.ndarrays
    :param mapping: dict
    :return: dict of np.ndarrays, np.ndarray
        bool arrays; True: connected (within same segment)
        isolated node ids
    """
    def _mapping_default(key):
        if key in mapping:
            return mapping[key]
        else:
            return -1

    mapping_vec = np.vectorize(_mapping_default)

    active = {}
    isolated = [[]]
    for k in edge_dict:
        if len(edge_dict[k]["sv1"]) > 0:
            agg_id_1 = mapping_vec(edge_dict[k]["sv1"])
        else:
            assert len(edge_dict[k]["sv2"]) == 0
            active[k] = np.array([], dtype=np.bool)
            continue

        agg_id_2 = mapping_vec(edge_dict[k]["sv2"])

        active[k] = agg_id_1 == agg_id_2

        # Set those with two -1 to False
        agg_1_m = agg_id_1 == -1
        agg_2_m = agg_id_2 == -1
        active[k][agg_1_m] = False

        isolated.append(edge_dict[k]["sv1"][agg_1_m])

        if k == "in":
            isolated.append(edge_dict[k]["sv2"][agg_2_m])

    return active, np.unique(np.concatenate(isolated).astype(basetypes.NODE_ID))

