from django.urls import path
from . import views

urlpatterns = [
    path('', views.planner_view, name='planner'),
    path('plans/', views.plan_list_view, name='plan_list'),
    path('plans/save/', views.plan_save_view, name='plan_save'),
    path('plans/<int:pk>/', views.plan_detail_view, name='plan_detail'),
]
