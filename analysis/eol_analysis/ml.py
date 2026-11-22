"""Models for one part: the golden envelope, failure-mode classifiers, and the decision.

Methods (each can be enabled and ordered per part, like a BIOS boot order):

  golden_envelope   two distances to the Golden OK samples, both must be small: Mahalanobis
                    (shrunk covariance) inside the PCA subspace of normal variation, AND the
                    residual off that subspace. The residual catches a change in a direction
                    good parts never vary in, which PCA alone would hide (found in the tests:
                    a fault 47 standard deviations out in one feature passed without it).
                    Always on: a part outside it can never be OK.
  isolation_forest  a second, different one-class check on the Golden OK samples, in the
                    envelope's reduced space, with a leave-one-out threshold. OFF by default:
                    with about 10 golden samples it rejects good parts too often (found in
                    the tests). Enable it from about 30 Golden OK samples.
  knn               k-nearest neighbours after PCA, over Golden OK and every failure mode
  svm               support vector machine (RBF kernel) over the same classes
  gboost            gradient-boosted trees over the same classes (needs more samples; off by default)

Each failure mode also gets its own envelope, the same way as the golden one.

The decision, safety first:
  OK         only if the part is inside the golden envelope AND every enabled method
             agrees it is good. Any enabled method can veto a PASS.
  NOK:<mode> otherwise, if a method (taken in priority order) names a failure mode with
             probability >= p_min AND the part lies inside that mode's envelope. The
             first method in the order that does so gives the reported mode.
  UNDEFINED  otherwise: outside every envelope, or the methods do not agree on a mode.
  drift      an OK part whose golden distance is above drift_fraction of the threshold
             (yellow CONTROLLED DRIFT: still OK, but closer to the edge than usual).

Cross-validation is grouped by physical unit (unit serial), so samples of one part never
sit on both sides of a split. Pruning is done inside each fold too, so the reported
escape and false-reject rates are not flattered by it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.covariance import LedoitWolf
from sklearn.decomposition import PCA
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from .catalogue import REGISTRY

OK = "OK"
METHODS = ("golden_envelope", "isolation_forest", "knn", "svm", "gboost")
DEFAULT_PRIORITY = [("golden_envelope", True), ("isolation_forest", False), ("knn", True), ("svm", True), ("gboost", False)]


@dataclass(frozen=True)
class TrainSettings:
    margin: float = 1.25  # envelope threshold = largest leave-one-out distance x margin
    p_min: float = 0.6  # a method must be this sure to name a failure mode
    drift_fraction: float = 0.8
    prune_d: float = 0.5  # an analysis is useful if one of its features separates a mode from golden by this many standard deviations
    min_golden: int = 5
    min_per_mode: int = 3
    seed: int = 0


def _parts(pca: PCA, mean: np.ndarray, prec: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(Mahalanobis distance in the subspace, residual distance off it) for rows z."""
    z = np.atleast_2d(z)
    y = pca.transform(z)
    d = y - mean
    t = np.sqrt(np.maximum(np.einsum("ij,jk,ik->i", d, prec, d), 0))
    q = np.linalg.norm(z - pca.inverse_transform(y), axis=1)
    return t, q


@dataclass
class Envelope:
    pca: PCA
    mean: np.ndarray
    precision: np.ndarray
    t_limit: float
    q_limit: float
    threshold: float = 1.0  # distance() is scaled so that the edge of the envelope is 1

    def distance(self, z: np.ndarray) -> np.ndarray:
        t, q = _parts(self.pca, self.mean, self.precision, z)
        return np.maximum(t / self.t_limit, q / self.q_limit)


def fit_envelope(z: np.ndarray, margin: float) -> Envelope:
    """Envelope around rows z (already standardised). Needs at least 3 rows. Each limit is the
    largest leave-one-out value times the margin."""
    n = len(z)
    if n < 3:
        raise ValueError("an envelope needs at least 3 samples")

    def build(rows):
        pca = PCA(n_components=min(len(rows) - 1, 8, rows.shape[1]), random_state=0).fit(rows)
        y = pca.transform(rows)
        cov = LedoitWolf().fit(y)
        return pca, y.mean(axis=0), np.linalg.pinv(cov.covariance_)

    t_loo, q_loo = [], []
    for i in range(n):
        t, q = _parts(*build(np.delete(z, i, axis=0)), z[i:i + 1])
        t_loo.append(float(t[0]))
        q_loo.append(float(q[0]))
    pca, mean, prec = build(z)
    return Envelope(pca, mean, prec, max(t_loo) * margin + 1e-9, max(q_loo) * margin + 1e-9)


@dataclass
class Decision:
    verdict: str
    failure_mode: str | None
    drift: bool
    golden_distance: float
    golden_threshold: float
    mode_distances: dict[str, tuple[float, float]]
    votes: list[dict]
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "failure_mode": self.failure_mode, "drift": self.drift,
                "golden_distance": round(self.golden_distance, 4), "golden_threshold": round(self.golden_threshold, 4),
                "mode_distances": {k: [round(a, 4), round(b, 4)] for k, (a, b) in self.mode_distances.items()},
                "votes": self.votes, "reasons": self.reasons}


@dataclass
class PartModel:
    feature_names: list[str]
    medians: np.ndarray
    scaler: StandardScaler
    golden: Envelope
    modes: dict[str, Envelope]
    classifiers: dict[str, object]
    isolation: "OneClassForest"
    settings: TrainSettings
    pruned: list[str] = field(default_factory=list)

    def _z(self, values: dict[str, float]) -> np.ndarray:
        x = np.array([values.get(k, math.nan) for k in self.feature_names], dtype=np.float64)
        x = np.where(np.isfinite(x), x, self.medians)
        return self.scaler.transform(x[None, :])[0]

    def decide(self, values: dict[str, float], priority: list[tuple[str, bool]] | None = None) -> Decision:
        priority = priority or DEFAULT_PRIORITY
        z = self._z(values)
        s = self.settings
        dg = float(self.golden.distance(z)[0])
        inside = dg <= self.golden.threshold
        mode_d = {m: (float(e.distance(z)[0]), e.threshold) for m, e in self.modes.items()}
        votes, reasons = [], []
        for method, enabled in priority:
            if method == "golden_envelope":
                v = {"method": method, "enabled": True, "top": OK if inside else "outside", "p": None}
            elif method == "isolation_forest":
                v = {"method": method, "enabled": enabled, "top": OK if self.isolation.normal(z) else "outlier", "p": None}
            elif method in self.classifiers:
                clf = self.classifiers[method]
                proba = clf.predict_proba(z[None, :])[0]
                k = int(np.argmax(proba))
                v = {"method": method, "enabled": enabled, "top": str(clf.classes_[k]), "p": round(float(proba[k]), 4)}
            else:
                continue
            votes.append(v)
        active = [v for v in votes if v["enabled"]]
        if all(v["top"] == OK for v in active):
            drift = dg > s.drift_fraction * self.golden.threshold
            if drift:
                reasons.append(f"golden distance {dg:.2f} is above {s.drift_fraction:.0%} of the threshold {self.golden.threshold:.2f}")
            return Decision(OK, None, drift, dg, self.golden.threshold, mode_d, votes, reasons)
        for v in active:
            if v["top"] != OK:
                reasons.append(f"{v['method']}: {v['top']}" + (f" (p {v['p']:.2f})" if v["p"] is not None else ""))
        for v in active:
            m = v["top"]
            if m in self.modes and v["p"] is not None and v["p"] >= s.p_min and mode_d[m][0] <= mode_d[m][1]:
                reasons.append(f"{m}: named by {v['method']} and inside its envelope")
                return Decision("NOK", m, False, dg, self.golden.threshold, mode_d, votes, reasons)
        inside_modes = sorted((d / t, m) for m, (d, t) in mode_d.items() if d <= t)
        if not inside and len(inside_modes) == 1:
            m = inside_modes[0][1]
            reasons.append(f"{m}: outside the golden envelope and inside only this mode's envelope")
            return Decision("NOK", m, False, dg, self.golden.threshold, mode_d, votes, reasons)
        reasons.append("outside the golden envelope" if not inside else "the methods do not agree on a failure mode")
        return Decision("UNDEFINED", None, False, dg, self.golden.threshold, mode_d, votes, reasons)


@dataclass
class TrainingData:
    values: list[dict[str, float]]
    labels: list[str]  # OK or the failure mode name
    groups: list[str]  # physical unit; one sample = its own group when unknown


def _matrix(data: TrainingData, names: list[str]) -> np.ndarray:
    return np.array([[v.get(k, math.nan) for k in names] for v in data.values], dtype=np.float64)


def usable_features(data: TrainingData) -> list[str]:
    """Features present and finite in every sample and not constant."""
    names = sorted(set.intersection(*(set(v) for v in data.values)))
    x = _matrix(data, names)
    finite = np.all(np.isfinite(x), axis=0)
    keep = finite & (np.where(finite, x, 0).std(axis=0) > 1e-12)
    return [n for n, k in zip(names, keep) if k]


def discrimination(data: TrainingData, names: list[str]) -> dict[str, float]:
    """Largest Cohen's d of each feature between Golden OK and any failure mode."""
    x = _matrix(data, names)
    labels = np.array(data.labels)
    g = x[labels == OK]
    best = np.zeros(len(names))
    for mode in sorted(set(data.labels) - {OK}):
        m = x[labels == mode]
        pooled = np.sqrt((g.var(axis=0, ddof=1) * (len(g) - 1) + m.var(axis=0, ddof=1) * (len(m) - 1)) / (len(g) + len(m) - 2)) + 1e-12
        best = np.maximum(best, np.abs(m.mean(axis=0) - g.mean(axis=0)) / pooled)
    return dict(zip(names, best))


def prune(data: TrainingData, names: list[str], settings: TrainSettings) -> tuple[list[str], list[str], dict]:
    """(kept features, pruned analyses, report). Integrity analyses are never pruned."""
    if len(set(data.labels) - {OK}) == 0:
        return names, [], {"note": "no failure modes yet: nothing to judge discrimination by, nothing pruned"}
    d = discrimination(data, names)
    by_analysis: dict[str, float] = {}
    for n, v in d.items():
        a = n.split(".", 1)[0]
        by_analysis[a] = max(by_analysis.get(a, 0.0), v)
    pruned = sorted(a for a, v in by_analysis.items() if v < settings.prune_d and REGISTRY.get(a) and REGISTRY[a].path != "integrity")
    kept = [n for n in names if n.split(".", 1)[0] not in pruned]
    report = {"best_d_by_analysis": {a: round(v, 3) for a, v in sorted(by_analysis.items(), key=lambda kv: -kv[1])},
              "threshold_d": settings.prune_d, "pruned": pruned}
    return kept, pruned, report


def train(data: TrainingData, settings: TrainSettings = TrainSettings()) -> tuple[PartModel, dict]:
    labels = np.array(data.labels)
    n_golden = int((labels == OK).sum())
    if n_golden < settings.min_golden:
        raise ValueError(f"{n_golden} Golden OK training samples; at least {settings.min_golden} are needed")
    for mode in sorted(set(data.labels) - {OK}):
        if (labels == mode).sum() < settings.min_per_mode:
            raise ValueError(f"failure mode {mode}: {(labels == mode).sum()} training samples; at least {settings.min_per_mode} are needed")
    names, pruned, report = prune(data, usable_features(data), settings)
    x = _matrix(data, names)
    medians = np.nanmedian(x, axis=0)
    x = np.where(np.isfinite(x), x, medians)
    scaler = StandardScaler().fit(x)
    z = scaler.transform(x)
    golden = fit_envelope(z[labels == OK], settings.margin)
    modes = {m: fit_envelope(z[labels == m], settings.margin) for m in sorted(set(data.labels) - {OK})}
    iso = OneClassForest.fit(golden, z[labels == OK], settings)
    classifiers = {}
    if len(set(data.labels)) >= 2:
        k = max(1, min(5, int(np.bincount(np.unique(labels, return_inverse=True)[1]).min()) - 1))
        lowd = min(len(z) - 1, 10, z.shape[1])
        knn = KNeighborsClassifier(n_neighbors=k, weights="distance")
        knn_pca = PCA(n_components=lowd, random_state=0).fit(z)
        classifiers["knn"] = _Piped(knn_pca, knn.fit(knn_pca.transform(z), labels))
        folds = max(2, min(3, int(np.bincount(np.unique(labels, return_inverse=True)[1]).min())))
        classifiers["svm"] = CalibratedClassifierCV(SVC(kernel="rbf", class_weight="balanced", random_state=settings.seed),
                                                    method="sigmoid", cv=folds, ensemble=False).fit(z, labels)
        classifiers["gboost"] = HistGradientBoostingClassifier(min_samples_leaf=3, max_iter=100, random_state=settings.seed).fit(z, labels)
    model = PartModel(names, medians, scaler, golden, modes, classifiers, iso, settings, pruned)
    return model, report


class OneClassForest:
    """Isolation forest in the golden envelope's PCA space; a part is normal if its score is no
    lower than the lowest leave-one-out score of the golden samples, less a margin."""

    def __init__(self, pca, forest, threshold):
        self.pca, self.forest, self.threshold = pca, forest, threshold

    @classmethod
    def fit(cls, envelope: Envelope, z_golden: np.ndarray, settings: TrainSettings) -> "OneClassForest":
        y = envelope.pca.transform(z_golden)

        def forest(rows):
            return IsolationForest(n_estimators=200, contamination="auto", random_state=settings.seed).fit(rows)

        loo = [float(forest(np.delete(y, i, axis=0)).score_samples(y[i:i + 1])[0]) for i in range(len(y))]
        low = min(loo)
        return cls(envelope.pca, forest(y), low - (settings.margin - 1) * abs(low))

    def normal(self, z: np.ndarray) -> bool:
        return float(self.forest.score_samples(self.pca.transform(z[None, :]))[0]) >= self.threshold


class _Piped:
    """PCA then a classifier, with the classifier's predict_proba and classes_."""

    def __init__(self, pca, clf):
        self.pca, self.clf = pca, clf
        self.classes_ = clf.classes_

    def predict_proba(self, z):
        return self.clf.predict_proba(self.pca.transform(z))


def cross_validate(data: TrainingData, settings: TrainSettings = TrainSettings(),
                   priority: list[tuple[str, bool]] | None = None) -> dict:
    """Grouped cross-validation. Returns escape rate, false-reject rate and the confusion of verdicts."""
    labels = np.array(data.labels)
    groups = np.array(data.groups)
    n_splits = min(5, len(set(groups)))
    for lab in set(data.labels):  # every class needs at least as many groups as folds
        n_splits = min(n_splits, len(set(groups[labels == lab])))
    notes = []
    if n_splits < 2:
        return {"folds": 0, "note": "fewer than two physical units in a class: grouped cross-validation is not possible. "
                                    "Record samples from more than one unit per class."}
    rows = []
    cv = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=settings.seed)
    for k, (tr, te) in enumerate(cv.split(np.zeros(len(labels)), labels, groups)):
        sub = TrainingData([data.values[i] for i in tr], [data.labels[i] for i in tr], [data.groups[i] for i in tr])
        try:
            model, _ = train(sub, settings)
        except ValueError as exc:
            notes.append(f"fold {k + 1} skipped: {exc}")
            continue
        for i in te:
            dec = model.decide(data.values[i], priority)
            rows.append((data.labels[i], dec.verdict, dec.failure_mode))
    true_nok = [r for r in rows if r[0] != OK]
    true_ok = [r for r in rows if r[0] == OK]
    escapes = sum(1 for r in true_nok if r[1] == OK)
    false_rejects = sum(1 for r in true_ok if r[1] != OK)
    right_mode = sum(1 for r in true_nok if r[1] == "NOK" and r[2] == r[0])
    confusion: dict[str, dict[str, int]] = {}
    for true, verdict, mode in rows:
        key = f"NOK:{mode}" if verdict == "NOK" else verdict
        confusion.setdefault(true, {}).setdefault(key, 0)
        confusion[true][key] += 1
    return {"folds": n_splits, "tested": len(rows), "notes": notes,
            "escape_rate": escapes / len(true_nok) if true_nok else None,
            "false_reject_rate": false_rejects / len(true_ok) if true_ok else None,
            "right_mode_rate": right_mode / len(true_nok) if true_nok else None,
            "undefined": sum(1 for r in rows if r[1] == "UNDEFINED"),
            "escapes": escapes, "false_rejects": false_rejects, "confusion": confusion}
