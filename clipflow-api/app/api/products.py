from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.billing_product import BillingProduct

router = APIRouter()


@router.get("/products")
def list_products(db: Session = Depends(get_db)):

    products = db.query(BillingProduct).filter(
        BillingProduct.is_active == True
    ).all()

    return [
        {
            "id": str(p.id),
            "code": p.code,
            "name": p.name,
            "price": float(p.price_amount),
            "currency": p.currency,
            "max_video_duration_sec": p.max_video_duration_sec,
            "max_shorts_generated": p.max_shorts_generated,
        }
        for p in products
    ]