# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    _inherit = 'pos.session'

    def action_pos_session_closing_control(
            self, balancing_account=False, amount_to_balance=0,
            bank_payment_method_diffs=None):
        # Antes de que el cierre pase los pedidos 'paid' a 'done': lo vendido
        # después de la última corrida del cron se factura acá. Tanto el cierre
        # desde el POS como el del backend pasan por este método.
        for session in self:
            session._auto_invoice_before_closing()
        return super().action_pos_session_closing_control(
            balancing_account, amount_to_balance, bank_payment_method_diffs)

    def _auto_invoice_before_closing(self):
        """Factura los recibos pendientes de la sesión. Nunca impide el
        cierre: si falla (AFIP caído, datos del pedido), queda en el log y
        la recuperación del cron los levanta después."""
        self.ensure_one()
        config = self.config_id
        if not config.auto_invoice_enabled or self.state == 'closed':
            return
        # Con pedidos en borrador el cierre se rechaza igual: se factura en
        # el reintento, cuando el cierre realmente va a avanzar.
        if any(order.state == 'draft' for order in self.order_ids):
            return
        # El cajero no tiene permiso de escritura sobre el log ni sobre la
        # configuración; el proceso es el mismo que corre el cron.
        PosOrder = self.env['pos.order'].sudo()
        try:
            PosOrder._auto_invoice_for_config(
                config.sudo(), dry_run=False, session=self.sudo(),
                trigger='session_close')
        except Exception as exc:
            _logger.exception(
                'POS auto invoice: fallo al facturar en el cierre de %s', self.name)
            self.env.cr.rollback()
            try:
                PosOrder._auto_invoice_log_failure(
                    config, exc, trigger='session_close')
                self.env.cr.commit()
            except Exception:
                self.env.cr.rollback()
                _logger.exception(
                    'POS auto invoice: no se pudo registrar el log de error')
