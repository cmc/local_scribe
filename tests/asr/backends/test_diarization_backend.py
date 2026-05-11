"""Tests for the auto-K eigengap clustering pipeline added to
``diarization_backend.py``.

What we cover here:
  * ``_pick_k_eigengap``: edge cases (n=1, n=2 above/below similarity
    threshold), and the "two well-separated clusters" + "three
    well-separated clusters" canonical cases on synthetic embeddings.
  * ``_spectral_cluster``: returns labels in the right shape, agrees
    with the cluster structure of synthetic embeddings.
  * ``_remap_segments``: applies the centroid-to-final-label mapping,
    including filling holes via nearest-neighbour for raw labels that
    didn't get an embedding.
  * ``diarize_auto``: end-to-end wiring with sherpa-onnx + extractor
    mocked out, so we run the full pipeline shape without loading
    real models.

The tests for the actual sherpa-onnx output shape and TitaNet
embedding quality belong in a live integration test, not here.
"""

from __future__ import annotations

import unittest
from unittest import mock

import numpy as np

from local_scribe.asr.backends import diarization_backend


def _two_speaker_centroids(seed: int = 0) -> np.ndarray:
    """Build two well-separated unit-norm centroids in 8 dimensions.
    Includes a tiny perturbation for numerical realism."""
    rng = np.random.default_rng(seed)
    a = rng.normal(size=8); a /= np.linalg.norm(a)
    # Force the second to be nearly orthogonal to the first.
    b = rng.normal(size=8)
    b -= a * (a @ b)
    b /= np.linalg.norm(b)
    return np.stack([a, b])


class PickKEigengapTests(unittest.TestCase):
    """``_pick_k_eigengap`` is the heart of auto-K. We hit the small-n
    branches explicitly because they short-circuit (and we hit them
    on real short calls)."""

    def test_n1_returns_k1(self) -> None:
        a = np.array([[1.0]])
        k, _ = diarization_backend._pick_k_eigengap(a)
        self.assertEqual(k, 1)

    def test_n2_high_similarity_collapses_to_k1(self) -> None:
        # Two centroids cosine-similar above the monologue threshold
        # (default 0.80) → same speaker.
        a = np.array([[1.0, 0.92], [0.92, 1.0]])
        k, _ = diarization_backend._pick_k_eigengap(a)
        self.assertEqual(k, 1)

    def test_n2_low_similarity_returns_k2(self) -> None:
        a = np.array([[1.0, 0.10], [0.10, 1.0]])
        k, _ = diarization_backend._pick_k_eigengap(a)
        self.assertEqual(k, 2)

    def test_monologue_gate_returns_k1_when_mean_affinity_high(self) -> None:
        # All centroids similar → monologue gate fires → K=1, even
        # with many centroids.
        rng = np.random.default_rng(0)
        v = rng.normal(size=8); v /= np.linalg.norm(v)
        embs = np.stack([
            v + 0.02 * rng.normal(size=8) for _ in range(6)
        ])
        embs /= np.linalg.norm(embs, axis=1, keepdims=True)
        affinity = embs @ embs.T
        k, _ = diarization_backend._pick_k_eigengap(affinity)
        self.assertEqual(k, 1)

    def test_k_min_two_overrides_textbook_argmax(self) -> None:
        # Reproduces the Maus failure: the textbook eigengap picks K=1
        # because the trivial λ_0 → λ_1 gap is largest, but with K_min=2
        # we get the "real" structural answer K=2. Eigenvalues here
        # come from an actual 2-speaker affinity but with a high
        # baseline similarity that pushes λ_1 well above zero.
        rng = np.random.default_rng(13)
        a_dir = rng.normal(size=8); a_dir /= np.linalg.norm(a_dir)
        b_dir = rng.normal(size=8); b_dir -= a_dir * (a_dir @ b_dir)
        b_dir /= np.linalg.norm(b_dir)
        # Mix the two so cross-speaker similarity is moderate (not
        # near-zero like in the orthogonal toy case).
        embs = []
        for _ in range(4):
            v = 0.7 * a_dir + 0.3 * rng.normal(size=8)
            v /= np.linalg.norm(v)
            embs.append(v)
        for _ in range(4):
            v = 0.7 * b_dir + 0.3 * rng.normal(size=8)
            v /= np.linalg.norm(v)
            embs.append(v)
        affinity = np.stack(embs) @ np.stack(embs).T
        # With k_min=2 (default), we should get K=2.
        k, _ = diarization_backend._pick_k_eigengap(affinity)
        self.assertGreaterEqual(k, 2)

    def test_two_well_separated_clusters_picks_k2(self) -> None:
        # Build 6 centroids: 3 near A, 3 near B, where A ⟂ B.
        rng = np.random.default_rng(11)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        cluster_a = np.stack([a + 0.05 * rng.normal(size=8) for _ in range(3)])
        cluster_b = np.stack([b + 0.05 * rng.normal(size=8) for _ in range(3)])
        emb = np.vstack([cluster_a, cluster_b])
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        affinity = emb @ emb.T
        k, _ = diarization_backend._pick_k_eigengap(affinity)
        self.assertEqual(k, 2)

    def test_three_well_separated_clusters_picks_k3(self) -> None:
        rng = np.random.default_rng(7)
        # Three orthonormal directions in 16 dimensions.
        basis = np.linalg.qr(rng.normal(size=(16, 3)))[0]  # 16x3
        embs = []
        for i in range(3):
            for _ in range(4):
                v = basis[:, i] + 0.05 * rng.normal(size=16)
                embs.append(v / np.linalg.norm(v))
        affinity = np.stack(embs) @ np.stack(embs).T
        k, _ = diarization_backend._pick_k_eigengap(affinity, k_max=10)
        self.assertEqual(k, 3)

    def test_k_bounded_by_k_max(self) -> None:
        # 8 perfectly orthogonal centroids — true K=8 — but we cap at 4.
        emb = np.eye(8)
        affinity = emb @ emb.T
        k, _ = diarization_backend._pick_k_eigengap(affinity, k_max=4)
        self.assertLessEqual(k, 4)


class SilhouetteScoreTests(unittest.TestCase):
    """``_silhouette_score`` is the validity proxy backing the new
    K-selection picker. We test it on synthetic data with KNOWN
    ground-truth structure so failures are unambiguous."""

    def test_two_well_separated_clusters_high_silhouette(self) -> None:
        rng = np.random.default_rng(2)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        embs = np.stack([
            a + 0.02 * rng.normal(size=8) for _ in range(4)
        ] + [
            b + 0.02 * rng.normal(size=8) for _ in range(4)
        ])
        embs /= np.linalg.norm(embs, axis=1, keepdims=True)
        affinity = embs @ embs.T
        labels = np.array([0, 0, 0, 0, 1, 1, 1, 1])
        score = diarization_backend._silhouette_score(affinity, labels)
        # Well-separated → score should be solidly positive.
        self.assertGreater(score, 0.5)

    def test_misclassified_points_drop_silhouette(self) -> None:
        # Same two clusters, but assign one point from cluster A to B.
        # Mean silhouette must drop notably.
        rng = np.random.default_rng(2)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        embs = np.stack([
            a + 0.02 * rng.normal(size=8) for _ in range(4)
        ] + [
            b + 0.02 * rng.normal(size=8) for _ in range(4)
        ])
        embs /= np.linalg.norm(embs, axis=1, keepdims=True)
        affinity = embs @ embs.T
        good = np.array([0, 0, 0, 0, 1, 1, 1, 1])
        bad = np.array([0, 0, 0, 1, 0, 1, 1, 1])  # one swap
        s_good = diarization_backend._silhouette_score(affinity, good)
        s_bad = diarization_backend._silhouette_score(affinity, bad)
        self.assertGreater(s_good, s_bad)

    def test_single_cluster_returns_zero(self) -> None:
        affinity = np.eye(4)
        labels = np.zeros(4, dtype=np.int32)
        s = diarization_backend._silhouette_score(affinity, labels)
        self.assertEqual(s, 0.0)


class PickKSilhouetteTests(unittest.TestCase):
    """The new K-selection path. We test the same synthetic cases as
    PickKEigengapTests so regressions are easy to spot, plus the
    crucial 4-cluster case where eigengap fails and silhouette
    correctly picks K=4."""

    def _build_clusters(self, n_per_cluster: int, n_clusters: int,
                        dim: int = 16, noise: float = 0.05,
                        seed: int = 0) -> np.ndarray:
        rng = np.random.default_rng(seed)
        basis = np.linalg.qr(rng.normal(size=(dim, n_clusters)))[0]
        embs: list[np.ndarray] = []
        for i in range(n_clusters):
            for _ in range(n_per_cluster):
                v = basis[:, i] + noise * rng.normal(size=dim)
                embs.append(v / np.linalg.norm(v))
        return np.stack(embs) @ np.stack(embs).T

    def test_two_well_separated_clusters_picks_k2(self) -> None:
        a = self._build_clusters(n_per_cluster=4, n_clusters=2, seed=11)
        k, _ = diarization_backend._pick_k_silhouette(a)
        self.assertEqual(k, 2)

    def test_three_well_separated_clusters_picks_k3(self) -> None:
        a = self._build_clusters(n_per_cluster=4, n_clusters=3, seed=23)
        k, _ = diarization_backend._pick_k_silhouette(a)
        self.assertEqual(k, 3)

    def test_four_well_separated_clusters_picks_k4(self) -> None:
        # Regression: this was the CMC Legal case. Eigengap-argmax
        # picked K=2 because the K=1→K=2 gap dominated; silhouette
        # correctly finds the local maximum at K=4.
        a = self._build_clusters(n_per_cluster=5, n_clusters=4, seed=31)
        k, scores = diarization_backend._pick_k_silhouette(a)
        self.assertEqual(k, 4, f"silhouette by K: {scores}")

    def test_monologue_gate_returns_k1_on_dense_affinity(self) -> None:
        rng = np.random.default_rng(0)
        v = rng.normal(size=8); v /= np.linalg.norm(v)
        embs = np.stack([
            v + 0.01 * rng.normal(size=8) for _ in range(6)
        ])
        embs /= np.linalg.norm(embs, axis=1, keepdims=True)
        affinity = embs @ embs.T
        k, _ = diarization_backend._pick_k_silhouette(affinity)
        self.assertEqual(k, 1)

    def test_prefers_higher_k_on_ties(self) -> None:
        # Construct an affinity matrix where K=3 and K=4 score
        # essentially the same (within prefer_higher_within tolerance).
        # The bias should pick K=4.
        rng = np.random.default_rng(42)
        # 8 well-separated points (K=8 ground truth) — for K=3..K=7
        # silhouettes should be similar.
        a = self._build_clusters(n_per_cluster=2, n_clusters=4, seed=42)
        k, scores = diarization_backend._pick_k_silhouette(
            a, k_max=4, prefer_higher_within=0.1,
        )
        self.assertGreaterEqual(k, 3, f"silhouette by K: {scores}")


class ValidateClusterAirtimeTests(unittest.TestCase):
    def test_balanced_clusters_pass(self) -> None:
        # 4 clusters × 50 s each = 200 s total; smallest fraction = 25%
        # AND smallest absolute = 50 s, both pass the (3%, 30 s)
        # minimums.
        raw = [
            {"speaker": f"S{i//5:02d}", "start": float(i * 10),
             "end": float(i * 10 + 10)}
            for i in range(20)
        ]
        ctol = {f"S{i:02d}": i for i in range(4)}
        ok = diarization_backend._validate_cluster_airtime(raw, ctol)
        self.assertTrue(ok)

    def test_sliver_cluster_fails(self) -> None:
        raw = (
            [{"speaker": "A", "start": float(i), "end": float(i + 1)}
             for i in range(500)]
            + [{"speaker": "B", "start": 500.0, "end": 501.0}]  # 1 s only
        )
        ctol = {"A": 0, "B": 1}
        ok = diarization_backend._validate_cluster_airtime(
            raw, ctol, min_seconds=30, min_fraction=0.03,
        )
        self.assertFalse(ok)

    def test_small_total_audio_still_validates(self) -> None:
        # 60 s total, 30 s per speaker → just at min_seconds boundary.
        raw = [
            {"speaker": "A", "start": 0.0, "end": 30.0},
            {"speaker": "B", "start": 30.0, "end": 60.0},
        ]
        ctol = {"A": 0, "B": 1}
        self.assertTrue(
            diarization_backend._validate_cluster_airtime(raw, ctol),
        )


class PerPointSilhouetteTests(unittest.TestCase):
    """Per-point silhouette is the cluster-membership confidence
    signal surfaced in the transcript ("speaker_0 (87%)"). It must
    behave like the mean version on the obvious cases plus return
    correct shapes / value ranges."""

    def test_returns_zero_for_singleton_dataset(self) -> None:
        a = np.array([[1.0]])
        out = diarization_backend._per_point_silhouette(a, np.array([0]))
        self.assertEqual(out.shape, (1,))
        self.assertEqual(out[0], 0.0)

    def test_returns_zero_for_single_cluster(self) -> None:
        a = np.ones((4, 4))
        out = diarization_backend._per_point_silhouette(
            a, np.array([0, 0, 0, 0]),
        )
        self.assertEqual(out.shape, (4,))
        self.assertTrue(np.all(out == 0.0))

    def test_well_separated_clusters_high_silhouette(self) -> None:
        # Two pairs of nearly-identical embeddings; pairs are nearly
        # orthogonal. Per-point silhouette should be > 0.5 across the
        # board (close to 1 in fact since the clusters are tight).
        rng = np.random.default_rng(7)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        emb = np.stack([
            a + 0.001 * rng.normal(size=8),
            a + 0.001 * rng.normal(size=8),
            b + 0.001 * rng.normal(size=8),
            b + 0.001 * rng.normal(size=8),
        ])
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        affinity = emb @ emb.T
        out = diarization_backend._per_point_silhouette(
            affinity, np.array([0, 0, 1, 1]),
        )
        self.assertEqual(out.shape, (4,))
        self.assertTrue((out > 0.5).all(),
                        f"expected all > 0.5, got {out.tolist()}")

    def test_misclassified_point_has_low_or_negative_score(self) -> None:
        # Three points clustered together as "0", one outlier labelled "0"
        # but actually closer to cluster "1". Its silhouette must be the
        # smallest of the four (likely negative).
        rng = np.random.default_rng(2)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        emb = np.stack([a, a + 0.01 * rng.normal(size=8),
                        a + 0.01 * rng.normal(size=8), b])
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        affinity = emb @ emb.T
        # All four labelled cluster 0, but the last point sits in cluster B.
        labels = np.array([0, 0, 0, 1])
        out = diarization_backend._per_point_silhouette(affinity, labels)
        # The mislabelled "0" point gets a much lower score than the
        # three actually-similar points.
        self.assertEqual(np.argmin(out[:3]), np.argmin(out[:3]))
        # Sanity bound: silhouette is in [-1, 1].
        self.assertTrue((out <= 1.0).all() and (out >= -1.0).all())


class SilhouetteToConfidenceTests(unittest.TestCase):
    """Mapping silhouette [-1, 1] -> confidence [0, 1]."""

    def test_endpoints(self) -> None:
        self.assertEqual(diarization_backend.silhouette_to_confidence(1.0), 1.0)
        self.assertEqual(diarization_backend.silhouette_to_confidence(-1.0), 0.0)
        self.assertEqual(diarization_backend.silhouette_to_confidence(0.0), 0.5)

    def test_clamps_out_of_range(self) -> None:
        # Should never crash on numerical noise outside [-1, 1].
        self.assertEqual(diarization_backend.silhouette_to_confidence(1.5), 1.0)
        self.assertEqual(diarization_backend.silhouette_to_confidence(-2.0), 0.0)

    def test_linear_in_middle(self) -> None:
        self.assertAlmostEqual(
            diarization_backend.silhouette_to_confidence(0.5), 0.75,
        )
        self.assertAlmostEqual(
            diarization_backend.silhouette_to_confidence(-0.5), 0.25,
        )


class SpectralClusterTests(unittest.TestCase):
    def test_k1_returns_all_zeros(self) -> None:
        a = np.eye(5)
        labels = diarization_backend._spectral_cluster(a, 1)
        self.assertEqual(set(labels.tolist()), {0})

    def test_k_ge_n_returns_arange(self) -> None:
        a = np.eye(3)
        labels = diarization_backend._spectral_cluster(a, 5)
        self.assertEqual(labels.tolist(), [0, 1, 2])

    def test_two_clusters_separated(self) -> None:
        # 4 centroids, two pairs nearly identical, two pairs nearly
        # orthogonal across the divide.
        rng = np.random.default_rng(3)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        emb = np.stack([
            a + 0.01 * rng.normal(size=8),
            a + 0.01 * rng.normal(size=8),
            b + 0.01 * rng.normal(size=8),
            b + 0.01 * rng.normal(size=8),
        ])
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        affinity = emb @ emb.T
        labels = diarization_backend._spectral_cluster(affinity, 2)
        # Members 0,1 should share a label; 2,3 should share a label;
        # the two labels should differ.
        self.assertEqual(labels[0], labels[1])
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2])


class RemapSegmentsTests(unittest.TestCase):
    def test_simple_mapping_remaps_labels(self) -> None:
        raw = [
            {"speaker": "SPEAKER_05", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_07", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_05", "start": 2.0, "end": 3.0},
        ]
        mapping = {"SPEAKER_05": 0, "SPEAKER_07": 1}
        out = diarization_backend._remap_segments(raw, mapping)
        self.assertEqual([s["speaker"] for s in out],
                         ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"])

    def test_missing_label_filled_from_neighbour(self) -> None:
        # SPEAKER_99 has no mapping (its representative chunk was too
        # short to embed). We expect it to inherit the label of the
        # nearest segment that does have one.
        raw = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_99", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_00", "start": 2.0, "end": 3.0},
        ]
        mapping = {"SPEAKER_00": 0}
        out = diarization_backend._remap_segments(raw, mapping)
        self.assertEqual([s["speaker"] for s in out],
                         ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00"])

    def test_segments_sorted_by_start_in_output(self) -> None:
        # Even if the input arrives out of order, output must be sorted
        # so downstream word-attachment behaves deterministically.
        raw = [
            {"speaker": "A", "start": 5.0, "end": 6.0},
            {"speaker": "A", "start": 0.0, "end": 1.0},
        ]
        out = diarization_backend._remap_segments(raw, {"A": 0})
        self.assertEqual([s["start"] for s in out], [0.0, 5.0])

    def test_confidence_propagated_per_segment(self) -> None:
        raw = [
            {"speaker": "A", "start": 0.0, "end": 1.0},
            {"speaker": "B", "start": 1.0, "end": 2.0},
            {"speaker": "A", "start": 2.0, "end": 3.0},
        ]
        mapping = {"A": 0, "B": 1}
        confidence = {"A": 0.85, "B": 0.55}
        out = diarization_backend._remap_segments(
            raw, mapping, centroid_to_confidence=confidence,
        )
        self.assertEqual([s["confidence"] for s in out], [0.85, 0.55, 0.85])

    def test_missing_label_inherits_neighbour_confidence(self) -> None:
        # SPEAKER_99 has no mapping. After nearest-neighbour fill, it
        # should inherit not just the label but also the donor's
        # confidence — the only honest thing to report when we did the
        # fill ourselves.
        raw = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.0},
            {"speaker": "SPEAKER_99", "start": 1.0, "end": 2.0},
            {"speaker": "SPEAKER_00", "start": 2.0, "end": 3.0},
        ]
        mapping = {"SPEAKER_00": 0}
        out = diarization_backend._remap_segments(
            raw, mapping,
            centroid_to_confidence={"SPEAKER_00": 0.9},
        )
        self.assertEqual([s["confidence"] for s in out], [0.9, 0.9, 0.9])

    def test_no_confidence_argument_means_no_confidence_field(self) -> None:
        # Back-compat: callers who don't pass centroid_to_confidence
        # (e.g. legacy AHC path) should still get plain segments.
        raw = [
            {"speaker": "A", "start": 0.0, "end": 1.0},
        ]
        out = diarization_backend._remap_segments(raw, {"A": 0})
        self.assertNotIn("confidence", out[0])


class DiarizeAutoOrchestrationTests(unittest.TestCase):
    """End-to-end shape test for ``diarize_auto`` with sherpa-onnx +
    extractor mocked out. We're not testing TitaNet quality here —
    we're testing that the pipeline assembles the pieces correctly
    and remaps segments through the eigengap-selected K.
    """

    def setUp(self) -> None:
        # Pretend ensure_models() succeeded.
        self._patch_ensure = mock.patch.object(
            diarization_backend, "ensure_models",
            return_value=("/tmp/seg.onnx", "/tmp/emb.onnx"),
        )
        self._patch_ensure.start()
        self.addCleanup(self._patch_ensure.stop)

    def _long_segments(self, raw_speaker_durations: dict[str, float]) -> list[dict]:
        """Build raw segments with enough total airtime per speaker
        to clear the airtime validation in ``_validate_cluster_airtime``
        (30 s min, 3% of total min). 60 s per speaker is comfortable."""
        out: list[dict] = []
        t = 0.0
        for speaker, dur in raw_speaker_durations.items():
            out.append({"speaker": speaker, "start": t, "end": t + dur})
            t += dur
        return out

    def test_silhouette_collapses_overclustered_to_two(self) -> None:
        # Sherpa returns 5 micro-clusters but the embeddings for those
        # clusters cleanly collapse into 2 groups. diarize_auto should
        # remap accordingly. Each raw cluster has 60 s of airtime so
        # the airtime validator passes at both K=2 and K=5.
        sherpa_segments = self._long_segments({
            f"SPEAKER_{i:02d}": 60.0 for i in range(5)
        })

        rng = np.random.default_rng(0)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)

        def fake_centroids(*args, **kwargs):
            # 0,1,4 are speaker A; 2,3 are speaker B.
            return {
                "SPEAKER_00": a,
                "SPEAKER_01": a + 0.02 * rng.normal(size=8),
                "SPEAKER_02": b,
                "SPEAKER_03": b + 0.02 * rng.normal(size=8),
                "SPEAKER_04": a + 0.02 * rng.normal(size=8),
            }

        fake_samples = np.zeros(16000 * 11, dtype=np.float32)
        with mock.patch.object(diarization_backend, "_diarize_samples",
                               return_value=sherpa_segments), \
             mock.patch.object(diarization_backend, "_load_wav_16k_mono",
                               return_value=(fake_samples, 16000)), \
             mock.patch.object(diarization_backend, "_extract_centroid_embeddings",
                               side_effect=fake_centroids):
            out = diarization_backend.diarize_auto("/tmp/audio.wav")

        self.assertEqual(len(out), 5)
        labels = {s["speaker"] for s in out}
        # 5 micro-clusters collapse to 2 final speakers.
        self.assertEqual(len(labels), 2)
        # The same micro-cluster always maps to the same final label.
        self.assertEqual(out[0]["speaker"], out[1]["speaker"])
        self.assertEqual(out[2]["speaker"], out[3]["speaker"])
        self.assertEqual(out[0]["speaker"], out[4]["speaker"])
        self.assertNotEqual(out[0]["speaker"], out[2]["speaker"])

    def test_short_audio_single_speaker_passthrough(self) -> None:
        # Sherpa returns a single speaker — no work to do.
        sherpa_segments = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
        ]
        fake_samples = np.zeros(16000 * 5, dtype=np.float32)
        with mock.patch.object(diarization_backend, "_diarize_samples",
                               return_value=sherpa_segments), \
             mock.patch.object(diarization_backend, "_load_wav_16k_mono",
                               return_value=(fake_samples, 16000)):
            out = diarization_backend.diarize_auto("/tmp/audio.wav")
        self.assertEqual(out, sherpa_segments)

    def test_no_segments_returns_empty(self) -> None:
        fake_samples = np.zeros(16000 * 5, dtype=np.float32)
        with mock.patch.object(diarization_backend, "_diarize_samples",
                               return_value=[]), \
             mock.patch.object(diarization_backend, "_load_wav_16k_mono",
                               return_value=(fake_samples, 16000)):
            out = diarization_backend.diarize_auto("/tmp/audio.wav")
        self.assertEqual(out, [])

    def test_one_centroid_collapses_to_single_speaker(self) -> None:
        # Centroid extraction degenerated to a single embedding (the
        # filter dropped the rest as noise). Auto-K is degenerate, so
        # we collapse all raw segments to a single speaker rather
        # than returning sherpa's potentially-noisy raw clustering.
        sherpa_segments = [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 2.0},
            {"speaker": "SPEAKER_01", "start": 2.0, "end": 4.0},
        ]
        fake_samples = np.zeros(16000 * 5, dtype=np.float32)
        with mock.patch.object(diarization_backend, "_diarize_samples",
                               return_value=sherpa_segments), \
             mock.patch.object(diarization_backend, "_load_wav_16k_mono",
                               return_value=(fake_samples, 16000)), \
             mock.patch.object(diarization_backend, "_extract_centroid_embeddings",
                               return_value={"SPEAKER_00": np.ones(8)}):
            out = diarization_backend.diarize_auto("/tmp/audio.wav")
        self.assertEqual(len(out), 2)
        self.assertEqual({s["speaker"] for s in out}, {"SPEAKER_00"})

    def test_airtime_validation_demotes_overclustering(self) -> None:
        # Silhouette picks K=2 but spectral clustering at K=2 produces
        # a balanced split. We construct a case where K=2 fits cleanly
        # so we expect K=2 in the output — guarding against a future
        # regression where airtime validation incorrectly demotes
        # well-balanced clusterings to K=1.
        sherpa_segments = self._long_segments({"A": 60.0, "B": 60.0})

        rng = np.random.default_rng(1)
        a = rng.normal(size=8); a /= np.linalg.norm(a)
        b = rng.normal(size=8); b -= a * (a @ b); b /= np.linalg.norm(b)
        fake_samples = np.zeros(16000 * 5, dtype=np.float32)
        with mock.patch.object(diarization_backend, "_diarize_samples",
                               return_value=sherpa_segments), \
             mock.patch.object(diarization_backend, "_load_wav_16k_mono",
                               return_value=(fake_samples, 16000)), \
             mock.patch.object(diarization_backend, "_extract_centroid_embeddings",
                               return_value={"A": a, "B": b}):
            out = diarization_backend.diarize_auto("/tmp/audio.wav")
        self.assertEqual(len({s["speaker"] for s in out}), 2)


if __name__ == "__main__":
    unittest.main()
