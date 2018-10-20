import sys
import os
import numpy as np
import json
import re
import time

from cloudvolume import CloudVolume, Storage, EmptyVolumeException
from cloudvolume.meshservice import decode_mesh_buffer
from functools import lru_cache
from igneous.tasks import MeshTask

sys.path.insert(0, os.path.join(sys.path[0], '../..'))
os.environ['TRAVIS_BRANCH'] = "IDONTKNOWWHYINEEDTHIS"

from pychunkedgraph.backend.chunkedgraph import ChunkedGraph  # noqa
from pychunkedgraph.backend.key_utils import serialize_uint64  # noqa


def str_to_slice(slice_str: str):
    match = re.match(r"(\d+)-(\d+)_(\d+)-(\d+)_(\d+)-(\d+)", slice_str)
    return (slice(int(match.group(1)), int(match.group(2))),
            slice(int(match.group(3)), int(match.group(4))),
            slice(int(match.group(5)), int(match.group(6))))


def slice_to_str(slices) -> str:
    if isinstance(slices, slice):
        return "%d-%d" % (slices.start, slices.stop)
    else:
        return '_'.join(map(slice_to_str, slices))


def get_connected(connectivity):
    u_ids, c_ids = np.unique(connectivity, return_counts=True)
    return u_ids[(c_ids % 2) == 1].astype(np.uint64)


def get_chunk_bbox(cg: ChunkedGraph, chunk_id: np.uint64, mip: int):
    layer = cg.get_chunk_layer(chunk_id)
    chunk_block_shape = get_mesh_block_shape(cg, layer, mip)
    bbox_start = cg.get_chunk_coordinates(chunk_id) * chunk_block_shape
    bbox_end = bbox_start + chunk_block_shape
    return tuple(slice(bbox_start[i], bbox_end[i]) for i in range(3))


def get_chunk_bbox_str(cg: ChunkedGraph, chunk_id: np.uint64, mip: int) -> str:
    return slice_to_str(get_chunk_bbox(cg, chunk_id, mip))


@lru_cache(maxsize=None)
def get_segmentation_info(cg: ChunkedGraph) -> dict:
    return CloudVolume(cg.cv_path).info


def get_mesh_block_shape(cg: ChunkedGraph, graphlayer: int, source_mip: int) -> np.ndarray:
    """
    Calculate the dimensions of a segmentation block at `source_mip` that covers
    the same region as a ChunkedGraph chunk at layer `graphlayer`.
    """
    info = get_segmentation_info(cg)

    # Segmentation is not always uniformly downsampled in all directions.
    scale_0 = info['scales'][0]
    scale_mip = info['scales'][source_mip]
    distortion = np.floor_divide(scale_mip['resolution'], scale_0['resolution'])

    graphlayer_chunksize = cg.chunk_size * cg.fan_out ** np.max([0, graphlayer - 2])

    return np.floor_divide(graphlayer_chunksize, distortion, dtype=np.int, casting='unsafe')


def _get_sv_to_node_mapping_internal(cg, chunk_id, unbreakable_only):
    x, y, z = cg.get_chunk_coordinates(chunk_id)

    center_chunk_max_node_id = cg.get_node_id(
        segment_id=cg.get_segment_id_limit(chunk_id), chunk_id=chunk_id)
    xyz_plus_chunk_min_node_id = cg.get_chunk_id(
        layer=1, x=x + 1, y=y + 1, z=z + 1)

    row_keys = ['atomic_partners', 'connected']
    if unbreakable_only:
        row_keys.append('affinities')

    seg_ids_center = cg.range_read_chunk(1, x, y, z, row_keys=row_keys)

    seg_ids_face_neighbors = {}
    seg_ids_face_neighbors.update(
        cg.range_read_chunk(1, x + 1, y, z, row_keys=row_keys))
    seg_ids_face_neighbors.update(
        cg.range_read_chunk(1, x, y + 1, z, row_keys=row_keys))
    seg_ids_face_neighbors.update(
        cg.range_read_chunk(1, x, y, z + 1, row_keys=row_keys))

    seg_ids_edge_neighbors = {}
    seg_ids_edge_neighbors.update(
        cg.range_read_chunk(1, x + 1, y + 1, z, row_keys=row_keys))
    seg_ids_edge_neighbors.update(
        cg.range_read_chunk(1, x, y + 1, z + 1, row_keys=row_keys))
    seg_ids_edge_neighbors.update(
        cg.range_read_chunk(1, x + 1, y, z + 1, row_keys=row_keys))

    # Retrieve all face-adjacent supervoxel
    one_hop_neighbors = {}
    for seg_id_b, data in seg_ids_center.items():
        data = cg.flatten_row_dict(data.cells['0'])
        partners = data['atomic_partners']
        connected = get_connected(data['connected'])
        partners = partners[connected]

        # Only keep supervoxel within the "positive" adjacent chunks
        # (and if specified only the unbreakable counterparts)
        if unbreakable_only:
            affinities = data['affinities']
            affinities = affinities[connected]

            partners = partners[(affinities == np.inf) &
                                (partners > center_chunk_max_node_id)]
        else:
            partners = partners[partners > center_chunk_max_node_id]

        one_hop_neighbors.update(dict(zip(partners, [seg_id_b] * len(partners))))

    # Retrieve all edge-adjacent supervoxel
    two_hop_neighbors = {}
    for seg_id, base_id in one_hop_neighbors.items():
        seg_id_b = serialize_uint64(seg_id)
        if seg_id_b not in seg_ids_face_neighbors:
            # FIXME: Those are non-face-adjacent atomic partners caused by human
            #        proofreaders. They're crazy. Watch out for them.
            continue
        data = cg.flatten_row_dict(seg_ids_face_neighbors[seg_id_b].cells['0'])
        partners = data['atomic_partners']
        connected = get_connected(data['connected'])
        partners = partners[connected]

        # FIXME: The partners filter also keeps some connections to within the
        # face_adjacent chunk itself and to "negative", face-adjacent chunks.
        # That's OK for now, since the filters are only there to keep the
        # number of connections low
        if unbreakable_only:
            affinities = data['affinities']
            affinities = affinities[connected]

            partners = partners[(affinities == np.inf) &
                                (partners > center_chunk_max_node_id) &
                                (partners < xyz_plus_chunk_min_node_id)]
        else:
            partners = partners[(partners > center_chunk_max_node_id) &
                                (partners < xyz_plus_chunk_min_node_id)]

        two_hop_neighbors.update(dict(zip(partners, [base_id] * len(partners))))

    # Retrieve all corner-adjacent supervoxel
    three_hop_neighbors = {}
    for seg_id, base_id in list(two_hop_neighbors.items()):
        seg_id_b = serialize_uint64(seg_id)
        if seg_id_b not in seg_ids_edge_neighbors:
            # See FIXME for edge-adjacent supervoxel - need to ignore those
            # FIXME 2: Those might also be non-face-adjacent atomic partners
            # caused by human proofreaders.
            del two_hop_neighbors[seg_id]
            continue

        data = cg.flatten_row_dict(seg_ids_edge_neighbors[seg_id_b].cells['0'])
        partners = data['atomic_partners']
        connected = get_connected(data['connected'])
        partners = partners[connected]

        # We are only interested in the single corner voxel, but based on the
        # neighboring supervoxels, there might be a few more - doesn't matter.
        if unbreakable_only:
            affinities = data['affinities']
            affinities = affinities[connected]

            partners = partners[(affinities == np.inf) &
                                (partners > xyz_plus_chunk_min_node_id)]
        else:
            partners = partners[partners > xyz_plus_chunk_min_node_id]

        three_hop_neighbors.update(dict(zip(partners, [base_id] * len(partners))))

    sv_to_node_mapping = {seg_id: seg_id for seg_id in
                          [np.uint64(x) for x in seg_ids_center.keys()]}
    sv_to_node_mapping.update(one_hop_neighbors)
    sv_to_node_mapping.update(two_hop_neighbors)
    sv_to_node_mapping.update(three_hop_neighbors)
    sv_to_node_mapping = {np.uint64(k): np.uint64(v) for k, v in sv_to_node_mapping.items()}

    return sv_to_node_mapping


def get_sv_to_node_mapping(cg, chunk_id):
    """ Reads sv_id -> root_id mapping for a chunk from the chunkedgraph

    :param cg: chunkedgraph instance
    :param chunk_id: uint64
    :return: dict
    """

    layer = cg.get_chunk_layer(chunk_id)
    assert layer <= 2

    if layer == 1:
        # Mapping a chunk containing supervoxels - need to retrieve
        # potential unbreakable counterparts for each supervoxel
        # (Look for atomic edges with infinite edge weight)
        # Also need to check the edges and the corner!
        return _get_sv_to_node_mapping_internal(cg, chunk_id, True)

    else:  # layer == 2
        # Get supervoxel mapping
        x, y, z = cg.get_chunk_coordinates(chunk_id)
        sv_to_node_mapping = _get_sv_to_node_mapping_internal(
                cg, cg.get_chunk_id(layer=1, x=x, y=y, z=z), False)

        # Update supervoxel with their parents
        seg_ids = cg.range_read_chunk(1, x, y, z, row_keys=['parents'])

        for sv_id, base_sv_id in sv_to_node_mapping.items():
            base_sv_id = serialize_uint64(base_sv_id)
            data = cg.flatten_row_dict(seg_ids[base_sv_id].cells['0'])
            agg_id = data['parents'][-1]  # latest parent

            sv_to_node_mapping[sv_id] = agg_id

        return sv_to_node_mapping


def merge_meshes(meshes):
    num_vertices = 0
    vertices, faces = [], []

    # Dumb merge
    for mesh in meshes:
        vertices.extend(mesh['vertices'])
        faces.extend([x + num_vertices for x in mesh['faces']])
        num_vertices += len(mesh['vertices'])

    if num_vertices > 0:
        # Remove duplicate vertices
        vertex_representation = np.array(vertices)[faces]
        vertices, faces = np.unique(vertex_representation,
                                    return_inverse=True, axis=0)

    return {
        'num_vertices': len(vertices),
        'vertices': list(map(tuple, vertices)),
        'faces': list(faces)
    }


def chunk_mesh_task(cg, chunk_id, cv_path,
                    cv_mesh_dir=None, mip=3, max_err=40):
    """ Computes the meshes for a single chunk

    :param cg: ChunkedGraph instance
    :param chunk_id: int
    :param cv_path: str
    :param cv_mesh_dir: str or None
    :param mip: int
    :param max_err: float
    """

    layer = cg.get_chunk_layer(chunk_id)
    cx, cy, cz = cg.get_chunk_coordinates(chunk_id)

    if layer <= 2:
        # Level 1 or 2 chunk - fetch supervoxel mapping from ChunkedGraph, and
        # generate an igneous MeshTask, which will:
        # 1) Relabel the segmentation based on the sv_to_node_mapping
        # 2) Run Marching Cubes,
        # 3) simply each mesh using error quadrics to control the edge collapse
        # 4) upload a single mesh file for each segment of this chunk
        # 5) upload a manifest file for each segment of this chunk,
        #    pointing to the mesh for the segment

        print("Retrieving remap table for chunk %s -- (%s, %s, %s, %s)" % (chunk_id, layer, cx, cy, cz))
        sv_to_node_mapping = get_sv_to_node_mapping(cg, chunk_id)
        print("Remapped %s segments to %s agglomerations. Start meshing..." % (len(sv_to_node_mapping), len(np.unique(list(sv_to_node_mapping.values())))))

        if len(sv_to_node_mapping) == 0:
            print("Nothing to do", cx, cy, cz)
            return

        mesh_block_shape = get_mesh_block_shape(cg, layer, mip)

        chunk_offset = (cx, cy, cz) * mesh_block_shape

        task = MeshTask(
            mesh_block_shape,
            chunk_offset,
            cv_path,
            mip=mip,
            simplification_factor=999999,     # Simplify as much as possible ...
            max_simplification_error=max_err,  # ... staying below max error.
            remap_table=sv_to_node_mapping,
            generate_manifests=True,
            low_padding=0,                    # One voxel overlap to exactly line up
            high_padding=1,                   # vertex boundaries.
            mesh_dir=cv_mesh_dir
        )
        task.execute()

        print("Layer %d -- finished:" % layer, cx, cy, cz)

    else:
        # Layer 3+ chunk
        # 1) Load all manifests of next lower layer for this chunk
        # 2a) Merge mesh fragments of child chunks (3 <= layer < n_layers-3), or
        # 2b) Combine the manifests without creating new meshes

        create_new_fragments: bool = layer < cg.n_layers - 2

        node_ids = cg.range_read_chunk(layer, cx, cy, cz, row_keys=['children'])

        manifests_to_fetch = {np.uint64(x): [] for x in node_ids.keys()}

        mesh_dir = cv_mesh_dir or get_segmentation_info(cg)['mesh']

        chunk_block_shape = get_mesh_block_shape(cg, layer, mip)
        bbox_start = cg.get_chunk_coordinates(chunk_id) * chunk_block_shape
        bbox_end = bbox_start + chunk_block_shape
        chunk_bbox_str = slice_to_str(slice(bbox_start[i], bbox_end[i]) for i in range(3))

        for node_id_b, data in node_ids.items():
            node_id = np.uint64(node_id_b)
            children = np.frombuffer(data.cells['0'][b'children'][0].value, dtype=np.uint64)
            manifests_to_fetch[node_id].extend((f'{c}:0' for c in children))

        with Storage(os.path.join(cg.cv_path, mesh_dir)) as storage:
            print("Downloading Manifests...")
            manifest_content = storage.get_files((m for manifests in manifests_to_fetch.values() for m in manifests))
            print("Decoding Manifests...")
            manifest_content = {x['filename']: json.loads(x['content']) for x in manifest_content if x['content'] is not None and x['error'] is None}

            if create_new_fragments:
                # Only collect fragment filenames for nodes which consist of
                # more than one fragment, skipping the ones without a manifest
                print("Collect fragments to download...")
                fragments_to_fetch = [
                    fragment for manifests in manifests_to_fetch.values()
                    if len(manifests) > 1
                    for manifest in manifests
                    if manifest in manifest_content
                    for fragment in manifest_content[manifest]['fragments']]

                print("Downloading Fragments...")
                fragments_content = storage.get_files(fragments_to_fetch)

                print("Decoding Fragments...")
                fragments_content = {x['filename']: decode_mesh_buffer(x['filename'], x['content']) for x in fragments_content if x['content'] is not None and x['error'] is None}

        fragments_to_upload = []
        manifests_to_upload = []
        for node_id, manifests in manifests_to_fetch.items():
            manifest_filename = f'{node_id}:0'
            fragment_filename = f'{node_id}:0:{chunk_bbox_str}'

            fragments = [
                fragment for manifest in manifests
                if manifest in manifest_content
                for fragment in manifest_content[manifest]['fragments']]

            if len(fragments) < 2 or not create_new_fragments:
                # Create a single new manifest without creating any new
                # mesh fragments (point to existing fragments instead)

                # Note: An empty list of fragments might have been caused by
                #       tiny supervoxels/agglomerations near chunk boundaries,
                #       when those "disappeared" during meshing of _downsampled_
                #       layer 1 or 2 chunks.
                fragments_str = ''
                if len(fragments) > 0:
                    fragments_str = '"' + '","'.join(fragments) + '"'

                manifests_to_upload.append((
                    manifest_filename,
                    '{"fragments": [%s]}' % fragments_str
                ))
            else:
                # Merge mesh fragments into one new mesh, removing duplicate
                # vertices
                mesh = merge_meshes(map(lambda x: fragments_content[x] if x in fragments_content else {'num_vertices': 0, 'vertices': [], 'faces': []}, fragments))

                fragments_to_upload.append((
                    fragment_filename,
                    b''.join([
                        np.uint32(mesh['num_vertices']).tobytes(),
                        np.array(mesh['vertices'], dtype=np.float32).tobytes(),
                        np.array(mesh['faces'], dtype=np.uint32).tobytes()
                    ])
                ))

                manifests_to_upload.append((
                    manifest_filename,
                    '{"fragments": ["%s"]}' % fragment_filename
                ))

        print("Uploading new manifests and fragments...")
        with Storage(os.path.join(cg.cv_path, mesh_dir)) as storage:
            storage.put_files(fragments_to_upload, content_type='application/octet-stream', compress=True, cache_control=False)
            storage.put_files(manifests_to_upload, content_type='application/json', compress=False, cache_control=False)
            print("Uploaded %s manifests and %s fragments (reusing %s fragments)"
                  % (len(manifests_to_upload),
                     len(fragments_to_upload),
                     len(manifests_to_upload) - len(fragments_to_upload)))


def mesh_lvl2_preview(cg, lvl2_node_id, supervoxel_ids=None, cv_path=None,
                      cv_mesh_dir=None, mip=2, simplification_factor=999999,
                      max_err=40, parallel_download=8, verbose=True,
                      cache_control=None):
    """ Compute a mesh for a level 2 node without hierarchy and without
        consistency beyond the chunk boundary. Useful to give the user a quick
        preview. A proper mesh hierarchy should be generated using
        `mesh_node_hierarchy()`

    :param cg: ChunkedGraph instance
    :param lvl2_node_id: int
    :param supervoxel_ids: list of np.uint64
    :param cv_path: str or None (cg.cv_path)
    :param cv_mesh_dir: str or None
    :param mip: int
    :param simplification_factor: int
    :param max_err: float
    :param parallel_download: int
    :param verbose: bool
    :param cache_control: cache_control
    """

    layer = cg.get_chunk_layer(lvl2_node_id)
    assert layer == 2

    if cv_path is None:
        cv_path = cg.cv_path

    if supervoxel_ids is None:
        supervoxel_ids = cg.get_subgraph_nodes(lvl2_node_id, verbose=verbose)

    remap_table = dict(zip(supervoxel_ids, [lvl2_node_id] * len(supervoxel_ids)))

    mesh_block_shape = get_mesh_block_shape(cg, layer, mip)

    cx, cy, cz = cg.get_chunk_coordinates(lvl2_node_id)
    chunk_offset = (cx, cy, cz) * mesh_block_shape

    task = MeshTask(
        mesh_block_shape,
        chunk_offset,
        cv_path,
        mip=mip,
        simplification_factor=simplification_factor,
        max_simplification_error=max_err,
        remap_table=remap_table,
        generate_manifests=True,
        low_padding=0,
        high_padding=0,
        mesh_dir=cv_mesh_dir,
        parallel_download=parallel_download,
        cache_control=cache_control
    )
    if verbose:
        time_start = time.time()

    task.execute()

    if verbose:
        print("Preview Mesh for layer 2 Node ID %d: %.3fms (%d supervoxel)" %
              (lvl2_node_id, (time.time() - time_start) * 1000, len(supervoxel_ids)))
    return


def run_task_bundle(settings, layer, roi):
    cgraph = ChunkedGraph(
        table_id=settings['chunkedgraph']['table_id'],
        instance_id=settings['chunkedgraph']['instance_id']
    )
    meshing = settings['meshing']
    mip = meshing.get('mip', 2)
    max_err = meshing.get('max_simplification_error', 40)
    mesh_dir = meshing.get('mesh_dir', None)

    base_chunk_span = int(cgraph.fan_out) ** max(0, layer - 2)
    chunksize = np.array(cgraph.chunk_size, dtype=np.int) * base_chunk_span

    for x in range(roi[0].start, roi[0].stop, chunksize[0]):
        for y in range(roi[1].start, roi[1].stop, chunksize[1]):
            for z in range(roi[2].start, roi[2].stop, chunksize[2]):
                chunk_id = cgraph.get_chunk_id_from_coord(layer, x, y, z)

                try:
                    chunk_mesh_task(cgraph, chunk_id, cgraph.cv_path,
                                    cv_mesh_dir=mesh_dir, mip=mip, max_err=max_err)
                except EmptyVolumeException as e:
                    print("Warning: Empty segmentation encountered: %s" % e)


if __name__ == "__main__":
    params = json.loads(sys.argv[1])
    layer = int(sys.argv[2])
    run_task_bundle(params, layer, str_to_slice(sys.argv[3]))
