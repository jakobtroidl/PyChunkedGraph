"""
Classes and types for edges
"""


import numpy as np

IN_CHUNK = "in"
BT_CHUNK = "between"
CX_CHUNK = "cross"
TYPES = [IN_CHUNK, BT_CHUNK, CX_CHUNK]


class Edges:
    def __init__(
        self,
        node_ids1: np.ndarray,
        node_ids2: np.ndarray,
        affinities: np.ndarray,
        areas: np.ndarray,
    ):
        self.node_ids1 = node_ids1
        self.node_ids2 = node_ids2
        self.affinities = affinities
        self.areas = areas
        self._as_pairs = None

    def get_pairs(self):
        """
        return numpy array of edge pairs [[sv1, sv2] ... ]
        """
        if not self._as_pairs is None:
            return self._as_pairs
        self._as_pairs = np.vstack([self.node_ids1, self.node_ids2]).T
        return self._as_pairs
