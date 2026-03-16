from django.urls import path

from archive.views import (
    ArchiveLoginView,
    ArchiveLogoutView,
    ItemCreateView,
    api_create_item,
    item_archived_audio,
    item_article_audio,
    item_detail,
    overview,
    podcast_feed,
    rss_feed,
    search,
)

app_name = "archive"

urlpatterns = [
    path("", overview, name="overview"),
    path("search/", search, name="search"),
    path("api/items/", api_create_item, name="api-items"),
    path("feeds/rss.xml", rss_feed, name="rss-feed"),
    path("feeds/rss/page/<int:page>.xml", rss_feed, name="rss-feed-page"),
    path("feeds/podcast.xml", podcast_feed, name="podcast-feed"),
    path("feeds/podcast/page/<int:page>.xml", podcast_feed, name="podcast-feed-page"),
    path("items/new/", ItemCreateView.as_view(), name="item-new"),
    path("items/<int:pk>/audio/", item_archived_audio, name="item-archived-audio"),
    path("items/<int:pk>/article-audio/", item_article_audio, name="item-article-audio"),
    path("items/<int:pk>/", item_detail, name="item-detail"),
    path("accounts/login/", ArchiveLoginView.as_view(), name="login"),
    path("accounts/logout/", ArchiveLogoutView.as_view(), name="logout"),
]
