from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModerationResult:
    decision: str
    flags: list[str]
    reason: Optional[str] = None


BLOCKED_TERMS = {
    "хуурамч",
    "хуулбар",
    "counterfeit",
    "fake",
    "зэвсэг",
    "мансууруулах",
}


def evaluate_product(
    *,
    title: str,
    description: str,
    price: float,
    stock: int,
    category: str,
    image_count: int,
    store_is_approved: bool,
) -> ModerationResult:
    """Deterministic first-pass moderation used by the seller API."""
    hard_flags: list[str] = []
    review_flags: list[str] = []
    combined = f"{title} {description}".lower()

    if len(title.strip()) < 3:
        hard_flags.append("TITLE_TOO_SHORT")
    if price <= 0:
        hard_flags.append("INVALID_PRICE")
    if stock < 0:
        hard_flags.append("INVALID_STOCK")
    if not category.strip():
        hard_flags.append("MISSING_CATEGORY")
    if any(term in combined for term in BLOCKED_TERMS):
        hard_flags.append("BLOCKED_TERM")

    if image_count < 1:
        hard_flags.append("MISSING_IMAGE")
    if not store_is_approved:
        review_flags.append("STORE_NOT_APPROVED")

    if hard_flags:
        reason = "Барааны мэдээлэл стандарт хангахгүй байна."
        if "MISSING_IMAGE" in hard_flags:
            reason = "Вэб дээр зарахад зураг хэрэгтэй."
        return ModerationResult("rejected", hard_flags + review_flags, reason)
    if review_flags:
        reason = None
        if "STORE_NOT_APPROVED" in review_flags:
            reason = "Лангуу админ баталгаажуулалт хүлээж байна."
        return ModerationResult("pending_review", review_flags, reason)
    return ModerationResult("published", [])
