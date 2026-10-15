# -*- coding: utf-8 -*-
{
    'name': 'POS: Facturación automática por porcentaje',
    'version': '19.0.1.1.0',
    'summary': 'Check Factura destildado por defecto y facturación automática de un % de las ventas del día a Consumidor Final',
    'category': 'Sales/Point of Sale',
    'author': 'Aftermoves',
    'depends': ['point_of_sale', 'l10n_ar_pos'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/pos_auto_invoice_log_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_auto_invoice_cafetres/static/src/**/*',
        ],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
