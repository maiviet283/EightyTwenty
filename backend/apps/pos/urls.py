"""
/api/v1/pos/... — one route per operation of the till's PosBackend (src/services/contracts.ts).
The till's VITE_API_BASE_URL is https://<host> and every path below starts with /api/v1/pos.
"""

from django.urls import path

from . import views as v

urlpatterns = [
    path("locations", v.LocationsView.as_view()),
    path("locations/<slug:slug>/categories", v.CategoriesView.as_view()),
    path("locations/<slug:slug>/menu", v.MenuView.as_view()),
    path("staff/verify-pin", v.VerifyPinView.as_view()),
    path("snapshot", v.SnapshotView.as_view()),
    path("orders", v.OrdersView.as_view()),
    path("orders/<str:order_id>/status", v.OrderStatusView.as_view()),
    path("orders/<str:order_id>/accept", v.AcceptView.as_view()),
    path("orders/<str:order_id>/preparation", v.PreparationView.as_view()),
    path("orders/<str:order_id>/adjustments", v.AdjustmentsView.as_view()),
    path("orders/<str:order_id>/tables", v.TablesView.as_view()),
    path("orders/<str:order_id>/payments", v.PaymentsView.as_view()),
    path("shifts", v.ShiftsView.as_view()),
    path("shifts/<str:shift_id>", v.ShiftView.as_view()),
    path("shifts/<str:shift_id>/moves", v.ShiftMovesView.as_view()),
    path("transactions", v.TransactionsView.as_view()),
    path("audit", v.AuditView.as_view()),
    path("customers/<str:customer_id>", v.CustomerView.as_view()),
    path("customers/<str:customer_id>/loyalty", v.CustomerLoyaltyView.as_view()),
    path("customers/<str:customer_id>/gift-card", v.CustomerGiftCardView.as_view()),
    path("items/<str:item_id>/sold-out", v.SoldOutView.as_view()),
    path(
        "locations/<slug:slug>/items/<str:item_id>/availability", v.CafeAvailabilityView.as_view()
    ),
    path("reservations/<str:reservation_id>", v.ReservationView.as_view()),
    path("catering/<str:enquiry_id>", v.CateringView.as_view()),
    path("locations/<slug:slug>/categories/order", v.CategoryOrderView.as_view()),
    path("locations/<slug:slug>/items/<slug:item_id>/price", v.ItemPriceView.as_view()),
    path("locations/<slug:slug>/modifiers/availability", v.ModifierAvailabilityView.as_view()),
    path("locations/<slug:slug>/settings/toggles", v.TillTogglesView.as_view()),
    path("customers/<str:customer_id>/loyalty/actions", v.CustomerLoyaltyActionView.as_view()),
]
