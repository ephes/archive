from django.urls import path

from archive.views import (
    ArchiveLoginView,
    ArchiveLogoutView,
    ItemCreateView,
    api_create_item,
    item_detail,
    overview,
)

app_name = "archive"

urlpatterns = [
    path("", overview, name="overview"),
    path("api/items/", api_create_item, name="api-items"),
    path("items/new/", ItemCreateView.as_view(), name="item-new"),
    path("items/<int:pk>/", item_detail, name="item-detail"),
    path("accounts/login/", ArchiveLoginView.as_view(), name="login"),
    path("accounts/logout/", ArchiveLogoutView.as_view(), name="logout"),
]
