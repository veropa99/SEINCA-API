# /integration_financiero_homologado/__manifest__.py
{
    "name": "Integración Financiero -> Destino",
    "version": "17.0.1.0.0",
    "summary": "Envía Pedidos de Venta y Compra a una base de datos destino mediante API.",
    "author": "Aecas",
    "website": "https://www.contablesag.com",
    "category": "Extra Tools",
    "depends": [
        "sale_management",
        "purchase",
    ],
    "data": [
        "views/res_config_settings_view.xml",
        "views/sale_order_view.xml",
        "views/purchase_order_view.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
}
