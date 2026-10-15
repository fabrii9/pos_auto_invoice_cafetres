# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class PosConfig(models.Model):
    _inherit = 'pos.config'

    auto_invoice_enabled = fields.Boolean(
        string='Facturación automática',
        help='Factura automáticamente un porcentaje de las ventas del día '
             '(pedidos que quedaron como recibo) a Consumidor Final.')
    auto_invoice_mode = fields.Selection(
        [('percent', 'Por porcentaje fijo'),
         ('payment_method', 'Por método de pago')],
        string='Modalidad', default='percent', required=True,
        help='Por porcentaje fijo: factura al azar pedidos hasta cubrir el '
             'porcentaje configurado del total del día en el diario AFIP, y el '
             'resto en el diario secundario.\n'
             'Por método de pago: el diario lo define cómo pagó el cliente, sin '
             'porcentaje ni azar. Un pedido pagado con un método asignado al '
             'diario AFIP se factura ahí; el resto va al diario secundario.')
    auto_invoice_percent = fields.Float(
        string='Porcentaje a facturar (%)', default=0.0,
        help='Porcentaje del total vendido en el día que debe quedar facturado. '
             'Se descuenta lo ya facturado manualmente desde el POS.')
    auto_invoice_journal_id = fields.Many2one(
        'account.journal', string='Diario AFIP (para el porcentaje)',
        domain=[('type', '=', 'sale')], check_company=True,
        help='Diario conectado a AFIP donde se factura el porcentaje configurado. '
             'Si está vacío, se usa el Diario de Facturas de este punto de venta.')
    auto_invoice_secondary_journal_id = fields.Many2one(
        'account.journal', string='Diario para el resto de las ventas',
        domain=[('type', '=', 'sale')], check_company=True,
        help='Diario SIN conexión a AFIP donde se factura el resto de las ventas '
             'del día (lo que excede el porcentaje). Si está vacío, el resto '
             'queda como recibo.')
    auto_invoice_afip_payment_method_ids = fields.Many2many(
        'pos.payment.method', 'pos_auto_invoice_afip_pm_rel', 'config_id', 'method_id',
        string='Métodos de pago del diario AFIP',
        help='Pedidos pagados con alguno de estos métodos se facturan en el '
             'diario AFIP. Solo se usa en la modalidad "Por método de pago".')
    auto_invoice_secondary_payment_method_ids = fields.Many2many(
        'pos.payment.method', 'pos_auto_invoice_secondary_pm_rel', 'config_id', 'method_id',
        string='Métodos de pago del diario secundario',
        help='Pedidos pagados con alguno de estos métodos se facturan en el '
             'diario secundario (sin AFIP). Solo se usa en la modalidad '
             '"Por método de pago".')
    auto_invoice_unmapped_to_secondary = fields.Boolean(
        string='Métodos sin asignar van al diario secundario', default=False,
        help='Qué hacer con un pedido cuyo método de pago no está en ninguna de '
             'las dos listas. Por defecto queda como recibo, sin facturar; '
             'activalo para mandarlo al diario secundario.')
    auto_invoice_secondary_on_afip_block = fields.Boolean(
        string='Facturar el resto aunque AFIP falle', default=False,
        help='Si AFIP está caído o hay un desfasaje de numeración, el porcentaje '
             'no se factura. Este diario no toca AFIP, así que puede seguir '
             'facturando igual. Por defecto se frena también, para conservar '
             'pedidos con los que cumplir el porcentaje en la próxima corrida.')
    auto_invoice_partner_id = fields.Many2one(
        'res.partner', string='Cliente Consumidor Final',
        help='Cliente que se asigna a los pedidos sin cliente al facturarlos '
             'automáticamente. Los pedidos que ya tienen cliente lo conservan.')
    auto_invoice_frequency = fields.Integer(
        string='Frecuencia (veces al día)', default=2,
        help='Cantidad de ejecuciones diarias del proceso automático para este '
             'punto de venta. Cada ejecución factura el porcentaje de lo vendido '
             'hasta el momento.')
    auto_invoice_max_per_run = fields.Integer(
        string='Máximo de facturas por ejecución', default=10,
        help='Tope de facturas por corrida. Con diarios AFIP cada validación '
             'puede tardar hasta 60 segundos; el tope evita corridas eternas.')
    auto_invoice_max_attempts = fields.Integer(
        string='Intentos por pedido', default=3,
        help='Cantidad de veces que se reintenta facturar un pedido que falla. '
             'Al superarla queda listado en el log para revisión manual.')
    auto_invoice_catchup_days = fields.Integer(
        string='Días para recuperar pedidos sin factura', default=7,
        help='Cuántos días hacia atrás se buscan pedidos que quedaron sin '
             'facturar en sesiones ya cerradas (por ejemplo, vendidos entre la '
             'última corrida y el cierre de caja). Se facturan con la fecha del '
             'día y Odoo revierte su parte del asiento de cierre de la sesión. '
             '0 desactiva la recuperación.')
    auto_invoice_last_run = fields.Datetime(
        string='Última ejecución automática', readonly=True, copy=False,
        help='Momento de la última corrida real (no simulación) de este punto '
             'de venta. Se usa para respetar la frecuencia configurada.')

    _auto_invoice_percent_range = models.Constraint(
        'CHECK(auto_invoice_percent >= 0 AND auto_invoice_percent <= 100)',
        'El porcentaje a facturar debe estar entre 0 y 100.')
    _auto_invoice_frequency_range = models.Constraint(
        'CHECK(auto_invoice_frequency >= 1 AND auto_invoice_frequency <= 24)',
        'La frecuencia debe estar entre 1 y 24 veces al día.')
    _auto_invoice_max_per_run_range = models.Constraint(
        'CHECK(auto_invoice_max_per_run >= 1 AND auto_invoice_max_per_run <= 100)',
        'El máximo por ejecución debe estar entre 1 y 100.')
    _auto_invoice_catchup_days_range = models.Constraint(
        'CHECK(auto_invoice_catchup_days >= 0 AND auto_invoice_catchup_days <= 90)',
        'Los días para recuperar pedidos deben estar entre 0 y 90.')
    _auto_invoice_max_attempts_range = models.Constraint(
        'CHECK(auto_invoice_max_attempts >= 1 AND auto_invoice_max_attempts <= 10)',
        'Los intentos por pedido deben estar entre 1 y 10.')

    @api.constrains('auto_invoice_secondary_journal_id')
    def _check_secondary_journal_not_afip(self):
        for config in self:
            journal = config.auto_invoice_secondary_journal_id
            if journal and journal._fields.get('l10n_ar_afip_ws') and journal.l10n_ar_afip_ws:
                raise ValidationError(_(
                    'El diario para el resto de las ventas no puede estar conectado '
                    'a AFIP (%s). Elegí un diario manual/electrónico sin web service.',
                    journal.name))

    @api.constrains('auto_invoice_journal_id', 'auto_invoice_secondary_journal_id')
    def _check_journals_are_different(self):
        for config in self:
            secondary = config.auto_invoice_secondary_journal_id
            afip = config.auto_invoice_journal_id or config.invoice_journal_id
            if secondary and afip and secondary == afip:
                raise ValidationError(_(
                    'El diario para el resto de las ventas no puede ser el mismo '
                    'que el diario del porcentaje (%s).', secondary.name))

    @api.constrains(
        'auto_invoice_mode',
        'auto_invoice_afip_payment_method_ids',
        'auto_invoice_secondary_payment_method_ids')
    def _check_payment_method_mapping(self):
        for config in self:
            if config.auto_invoice_mode != 'payment_method':
                continue
            overlap = (config.auto_invoice_afip_payment_method_ids
                       & config.auto_invoice_secondary_payment_method_ids)
            if overlap:
                raise ValidationError(_(
                    'Un método de pago no puede facturarse en los dos diarios a '
                    'la vez: %s', ', '.join(overlap.mapped('name'))))
            if config.auto_invoice_enabled and not (
                    config.auto_invoice_afip_payment_method_ids
                    or config.auto_invoice_secondary_payment_method_ids):
                raise ValidationError(_(
                    'En la modalidad "Por método de pago" hay que asignar al '
                    'menos un método de pago a alguno de los dos diarios.'))

    def _auto_invoice_is_due(self):
        """True si pasó el intervalo que corresponde a la frecuencia de este
        punto de venta. El cron es único y corre seguido; cada config decide
        si le toca, de modo que dos sucursales puedan tener cadencias
        distintas sin pisarse."""
        self.ensure_one()
        if not self.auto_invoice_last_run:
            return True
        frequency = max(1, min(24, self.auto_invoice_frequency or 1))
        interval = 24.0 / frequency
        elapsed = fields.Datetime.now() - self.auto_invoice_last_run
        # Margen de 5 minutos: el cron no dispara en el segundo exacto y si no
        # se tolera el desvío se pierde una corrida entera del día.
        return elapsed.total_seconds() >= interval * 3600 - 300

    def _auto_invoice_touch_last_run(self):
        self.ensure_one()
        self.sudo().write({'auto_invoice_last_run': fields.Datetime.now()})

    def action_simulate_auto_invoice(self):
        self.ensure_one()
        log = self.env['pos.order']._auto_invoice_for_config(
            self, dry_run=True, trigger='manual')
        return log.get_formview_action()

    def action_run_auto_invoice_now(self):
        self.ensure_one()
        log = self.env['pos.order']._auto_invoice_for_config(
            self, dry_run=False, trigger='manual')
        return log.get_formview_action()
