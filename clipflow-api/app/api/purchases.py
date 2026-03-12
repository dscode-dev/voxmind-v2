import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from fastapi import Request

from app.db.session import get_db
from app.models.billing_product import BillingProduct
from app.models.purchase import Purchase
from app.models.user import User
from app.security.auth_middleware import get_current_user
from app.models.enums import BillingProvider, PurchaseStatus

router = APIRouter()


@router.post("/purchases/checkout")
def create_checkout(
    product_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):

    product = db.query(BillingProduct).filter(
        BillingProduct.id == product_id,
        BillingProduct.is_active == True,
    ).first()

    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    purchase = Purchase(
        user_id=user.id,
        product_id=product.id,
        billing_provider=BillingProvider.STRIPE,
        status=PurchaseStatus.PENDING,
        currency=product.currency,
        amount_total=product.price_amount,
    )

    db.add(purchase)
    db.commit()
    db.refresh(purchase)

    # normalmente aqui criaríamos checkout no Stripe
    checkout_url = f"https://payments.clipflow.dev/{purchase.id}"

    purchase.provider_checkout_url = checkout_url

    db.commit()

    return {
        "purchase_id": str(purchase.id),
        "checkout_url": checkout_url,
    }
    
@router.post("/purchases/webhook")
async def purchase_webhook(
    request: Request,
    db: Session = Depends(get_db),
):

    payload = await request.json()

    purchase_id = payload.get("purchase_id")

    purchase = db.query(Purchase).filter(
        Purchase.id == purchase_id
    ).first()

    if not purchase:
        return {"status": "ignored"}

    purchase.status = PurchaseStatus.COMPLETED

    user = purchase.user
    product = purchase.product

    user.credits += 1
    user.token_version += 1

    db.commit()

    return {"status": "ok"}