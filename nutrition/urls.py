from django.urls import path
from django.views.generic import RedirectView
from . import account_views, views

urlpatterns = [
    path('accounts/', account_views.account_view, name='account'),
    path('accounts/register/', account_views.register_view, name='register'),
    path('accounts/login/', account_views.login_view, name='login'),
    path('accounts/logout/', account_views.logout_view, name='logout'),
    path('', views.planner_view, name='planner'),
    # Окремої сторінки «Ціль» більше нема: старі посилання ведуть на «Раціон» з тими самими параметрами
    path('goal/', RedirectView.as_view(pattern_name='planner', query_string=True)),
    path('goal/save/', views.goal_save_view, name='goal_save'),
    path('shopping/', views.shopping_view, name='shopping'),
    path('shopping/check/', views.shopping_check_view, name='shopping_check'),
    path('plans/', views.plan_list_view, name='plan_list'),
    path('plans/save/', views.plan_save_view, name='plan_save'),
    path('plans/<int:pk>/', views.plan_detail_view, name='plan_detail'),
    path('plans/<int:pk>/delete/', views.plan_delete_view, name='plan_delete'),
    path('silpo/login/', views.silpo_login_view, name='silpo_login'),
    path('silpo/callback/', views.silpo_callback_view, name='silpo_callback'),
    path('silpo/logout/', views.silpo_logout_view, name='silpo_logout'),
    path('silpo/prices/', views.silpo_prices_view, name='silpo_prices'),
    path('silpo/cart/', views.silpo_cart_view, name='silpo_cart'),
]
