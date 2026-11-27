FEATURES = [
 {"key":"dashboard_analytics_overview","group":"Dashboard","label":"Overview"},
 {"key":"dashboard_analytics_live_activity","group":"Dashboard","label":"Live activity"},
 {"key":"products_digital_inventory_product_catalogue","group":"Products","label":"Product catalogue"},
 {"key":"products_digital_inventory_bulk_product_import","group":"Products","label":"Bulk product import"},
 {"key":"orders_delivery_all_orders","group":"Orders","label":"All orders"},
 {"key":"orders_delivery_manual_delivery_retry","group":"Orders","label":"Manual delivery retry"},
 {"key":"payments_wallet_payment_settings","group":"Payments","label":"Payment settings"},
 {"key":"payments_wallet_refund_management","group":"Payments","label":"Refund management"},
 {"key":"customers_users_customer_directory","group":"Customers","label":"Customer directory"},
 {"key":"customers_users_block_customer","group":"Customers","label":"Block customer"},
 {"key":"coupons_marketing_coupon_manager","group":"Marketing","label":"Coupon manager"},
 {"key":"coupons_marketing_lucky_draw","group":"Marketing","label":"Lucky Draw System"},
 {"key":"support_notifications_support_inbox","group":"Support","label":"Support inbox"},
 {"key":"support_notifications_scheduled_announcements","group":"Support","label":"Scheduled announcements"},
 {"key":"admin_roles_permissions_admin_directory","group":"Admin Roles","label":"Admin directory"},
 {"key":"admin_roles_permissions_permission_matrix","group":"Admin Roles","label":"Permission matrix"},
 {"key":"reports_exports_sales_report","group":"Reports","label":"Sales report"},
 {"key":"security_backup_system_health_master_shop_emergency_switch","group":"Security","label":"Master shop emergency switch"},
 {"key":"advanced_referral_system","group":"Marketing","label":"Multi-Level Referral"},
 {"key":"spin_win_gamification","group":"Marketing","label":"Spin & Win"},
 {"key":"crypto_payment_gateway","group":"Payments","label":"Crypto Payments"},
 {"key":"abandoned_cart_recovery","group":"Marketing","label":"Abandoned Cart Alerts"}
]
FEATURE_KEYS = {item["key"] for item in FEATURES}
MASTER_KEY = "security_backup_system_health_master_shop_emergency_switch"
