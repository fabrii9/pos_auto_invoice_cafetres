# -*- coding: utf-8 -*-
from odoo import fields, models


class PosAutoInvoiceLog(models.Model):
    _name = 'pos.auto.invoice.log'
    _description = 'Log de facturación automática del POS'
    _order = 'create_date desc'

    name = fields.Char(string='Nombre', readonly=True)
    config_id = fields.Many2one(
        'pos.config', string='Punto de venta', readonly=True, ondelete='cascade')
    date = fields.Date(string='Fecha', readonly=True)
    is_simulation = fields.Boolean(string='Simulación', readonly=True)
    total_day = fields.Monetary(string='Total vendido del día', readonly=True)
    percent = fields.Float(string='% objetivo', readonly=True)
    target_amount = fields.Monetary(string='Monto objetivo', readonly=True)
    already_invoiced = fields.Monetary(string='Ya facturado (manual + automático)', readonly=True)
    invoiced_amount = fields.Monetary(string='Facturado en esta ejecución', readonly=True)
    order_count = fields.Integer(string='Pedidos facturados', readonly=True)
    secondary_invoiced_amount = fields.Monetary(
        string='Facturado al diario del resto', readonly=True)
    secondary_order_count = fields.Integer(
        string='Pedidos al diario del resto', readonly=True)
    secondary_selected_orders = fields.Text(
        string='Pedidos seleccionados (diario del resto)', readonly=True)
    error_count = fields.Integer(string='Errores', readonly=True)
    error_text = fields.Text(string='Detalle de errores', readonly=True)
    selected_orders = fields.Text(string='Pedidos seleccionados', readonly=True)
    afip_message = fields.Char(string='Verificación AFIP', readonly=True)
    afip_blocked = fields.Boolean(string='Bloqueado por AFIP', readonly=True)
    currency_id = fields.Many2one(
        related='config_id.currency_id', string='Moneda', readonly=True)
