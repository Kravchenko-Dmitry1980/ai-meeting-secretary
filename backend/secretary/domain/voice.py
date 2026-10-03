"""Private voice contracts. Never serialize these objects into public DTOs/logs."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from uuid import UUID

SAMPLE_RATE = 16000
DIMENSION = 192
MAX_SAMPLES = 30 * SAMPLE_RATE
MAX_PAYLOAD = 10 * 1024**2
TECHNICAL_POLICY_REVISION = "energy-screen-v1"


class VoiceError(Exception):
    """Allowlisted reason only: no paths, audio or subprocess diagnostics."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise VoiceError("invalid_identifier") from None
    return value


def valid_vector(vector) -> bool:
    if not isinstance(vector, (list, tuple)) or len(vector) != DIMENSION:
        return False
    try:
        if any(type(x) not in (float, int) or not math.isfinite(x) for x in vector):
            return False
        values = [float(x) for x in vector]
        largest = max(abs(x) for x in values)
        if largest == 0:
            return False
        # Scaling also makes validation total for mixed large integer/float
        # components: neither integer-to-float conversion nor norm may overflow.
        scaled_norm = math.sqrt(sum((x/largest)**2 for x in values))
        return largest > 1e-10 / scaled_norm
    except (OverflowError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class ModelStamp:
    model_id: str
    revision: str
    weights_sha256: str

    def __post_init__(self):
        if (self.model_id != "speechbrain/spkrec-ecapa-voxceleb"
                or len(self.revision) != 40 or len(self.weights_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.revision + self.weights_sha256)):
            raise VoiceError("invalid_model_metadata")


@dataclass(frozen=True)
class Excerpt:
    start_sample: int
    end_sample: int


@dataclass(frozen=True, repr=False)
class PreparedAudio:
    pcm16: bytes
    excerpts: tuple[Excerpt, ...]
    usable_samples: int
    review_reasons: tuple[str, ...] = ("listening_required", "single_speaker_review_required", "energy_screening_only")


@dataclass(frozen=True, repr=False)
class PrivateMaterial:
    profile_id: str
    enrollment_id: str
    material_revision: int
    model: ModelStamp
    audio: PreparedAudio
    embeddings: tuple[tuple[float, ...], ...] = field(repr=False)
