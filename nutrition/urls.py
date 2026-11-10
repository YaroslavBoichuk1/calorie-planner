from django.urls import path
from . import views

urlpatterns = [
    path('', views.planner_view, name='planner'),
    path('goal/', views.goal_view, name='goal'),
    path('shopping/', views.shopping_view, name='shopping'),
    path('plans/', views.plan_list_view, name='plan_list'),
    path('plans/save/', views.plan_save_view, name='plan_save'),
    path('plans/<int:pk>/', views.plan_detail_view, name='plan_detail'),
    path('silpo/login/', views.silpo_login_view, name='silpo_login'),
    path('silpo/callback/', views.silpo_callback_view, name='silpo_callback'),
    path('silpo/logout/', views.silpo_logout_view, name='silpo_logout'),
    path('silpo/prices/', views.silpo_prices_view, name='silpo_prices'),
    path('silpo/cart/', views.silpo_cart_view, name='silpo_cart'),
]
