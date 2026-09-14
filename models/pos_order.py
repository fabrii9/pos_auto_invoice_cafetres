# -*- coding: utf-8 -*-
import logging
import random
from datetime import datetime, time
from zoneinfo import ZoneInfo

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

SECONDARY_MAX_FACTOR = 10


class PosOrder(models.Model):
    _inherit = 'pos.order'

    auto_invoice_attempts = fields.Integer(
        string='Intentos de facturación automática', readonly=True, copy=False, default=0)
    auto_invoice_last_error = fields.Char(
        string='Último error de facturación automática', readonly=True, copy=False)

    def _prepare_invoice_vals(self):
        vals = super()._prepare_invoice_vals()
        auto_journal_id = self.env.context.get('auto_invoice_journal_id')
        if auto_journal_id:
            vals['journal_id'] = auto_journal_id
        return vals

    @api.model
    def _cron_auto_invoice(self):
        configs = self.env['pos.config'].search([('auto_invoice_enabled', '=', True)])
        for config in configs:
            if not config._auto_invoice_is_due():
                continue
            try:
                self._auto_invoice_for_config(config, dry_run=False)
            except Exception as exc:
                _logger.exception(
                    'POS auto invoice: fallo al procesar la configuración %s', config.name)
                # Registrar el fallo en el menú de Logs para que sea visible
                # en la interfaz y no quede solo en el log del servidor.
                # La transacción puede estar rota (errores crudos de AFIP),
                # así que se limpia antes de intentar escribir el log.
                self.env.cr.rollback()
                try:
                    self.env['pos.auto.invoice.log'].create({
                        'name': '%s - %s (error)' % (
                            config.name, fields.Date.today().strftime('%d/%m/%Y')),
                        'config_id': config.id,
                        'date': fields.Date.today(),
                        'error_count': 1,
                        'error_text': '%s: %s' % (type(exc).__name__, exc),
                    })
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        'POS auto invoice: no se pudo registrar el log de error')

    @api.model
    def _get_auto_invoice_day_bounds(self, config):
        # El cron corre como root (sin tz propia habitualmente), por eso la
        # referencia es la compañía del punto de venta y no el usuario: define
        # qué pedidos entran en "el día" y no debe depender de quién ejecute.
        tz_name = config.company_id.partner_id.tz or self.env.user.tz or 'UTC'
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            _logger.warning(
                'POS auto invoice: timezone inválida %r, se usa UTC', tz_name)
            tz = ZoneInfo('UTC')
        today = datetime.now(tz).date()
        utc = ZoneInfo('UTC')
        day_start = datetime.combine(today, time.min, tzinfo=tz).astimezone(utc).replace(tzinfo=None)
        day_end = datetime.combine(today, time.max, tzinfo=tz).astimezone(utc).replace(tzinfo=None)
        return today, day_start, day_end

    @api.model
    def _is_afip_ws_journal(self, journal):
        """True si el diario valida contra un web service de AFIP (l10n_ar_edi)."""
        return bool(journal) and bool(journal._fields.get('l10n_ar_afip_ws')) \
            and bool(journal.l10n_ar_afip_ws)

    @api.model
    def _auto_invoice_check_afip_numbering(self, journal):
        """Devuelve (ok, mensaje). Compara, POR CADA TIPO DE DOCUMENTO que el
        diario viene usando, la última factura posteada en Odoo contra el
        último número autorizado en AFIP.

        La numeración de AFIP es por punto de venta Y tipo de comprobante: un
        diario que emite Factura A y Factura B lleva dos contadores
        independientes, así que verificar solo el último comprobante emitido
        dejaría el otro tipo sin controlar.
        """
        # l10n_latam_document_number es computado no almacenado: no se puede
        # usar en el dominio, se filtra en Python.
        posted = self.env['account.move'].search([
            ('journal_id', '=', journal.id),
            ('state', '=', 'posted'),
            ('move_type', '=', 'out_invoice'),
            ('l10n_latam_document_type_id', '!=', False),
        ], order='id desc')
        if not posted:
            return True, _('Sin facturas previas en el diario: AFIP define la numeración inicial.')

        # Último movimiento numerado de cada tipo de documento presente.
        last_per_type = {}
        for move in posted:
            doc_type = move.l10n_latam_document_type_id
            if doc_type.id in last_per_type:
                continue
            if not move.l10n_latam_document_number:
                continue
            last_per_type[doc_type.id] = move

        if not last_per_type:
            return False, _(
                'Hay facturas posteadas en el diario pero ninguna tiene número de '
                'documento asignado. Se suspende la facturación automática hasta '
                'revisarlo manualmente.')

        messages = []
        for move in last_per_type.values():
            doc_type = move.l10n_latam_document_type_id
            try:
                last_afip = journal._l10n_ar_get_afip_last_invoice_number(doc_type)
            except Exception as exc:
                return False, _(
                    'No se pudo consultar AFIP para %(doc)s (%(error)s). Se suspende '
                    'la ejecución para evitar desfasajes de numeración.',
                    doc=doc_type.name, error=exc)
            # El WS devuelve False/0 cuando no hay comprobantes autorizados
            # (o en modo testing): no hay nada contra qué comparar.
            if not last_afip:
                messages.append(_(
                    '%(doc)s: AFIP no reporta comprobantes previos.', doc=doc_type.name))
                continue
            try:
                parts = move._l10n_ar_get_document_number_parts(
                    move.l10n_latam_document_number, doc_type.code)
                last_odoo = int(parts['invoice_number'])
            except Exception as exc:
                return False, _(
                    'No se pudo interpretar el número %(number)s de %(move)s '
                    '(%(error)s). Se suspende la facturación automática.',
                    number=move.l10n_latam_document_number, move=move.name, error=exc)
            if last_afip > last_odoo:
                return False, _(
                    'Desfasaje detectado: último número autorizado en AFIP = '
                    '%(afip)s, último en Odoo = %(odoo)s (%(move)s, tipo %(doc)s). '
                    'Se suspende la facturación automática hasta regularizarlo '
                    'manualmente.',
                    afip=last_afip, odoo=last_odoo, move=move.name, doc=doc_type.name)
            messages.append(_(
                '%(doc)s OK (AFIP: %(afip)s / Odoo: %(odoo)s).',
                doc=doc_type.name, afip=last_afip, odoo=last_odoo))
        return True, ' | '.join(messages)

    @api.model
    def _auto_invoice_one(self, order, config, journal):
        if not order.partner_id and config.auto_invoice_partner_id:
            order.partner_id = config.auto_invoice_partner_id
        order.with_context(
            generate_pdf=False,
            auto_invoice_journal_id=journal.id,
        )._generate_pos_order_invoice()

    @api.model
    def _auto_invoice_mark_error(self, order, exc, transaction_broken):
        """Registra el intento fallido en el pedido. Si la transacción quedó
        rota (errores crudos de red de AFIP), la limpia antes de escribir y
        commitea para no perder el contador de intentos."""
        if transaction_broken:
            self.env.cr.rollback()
            order = self.browse(order.id)
            if not order.exists():
                return
        order.sudo().write({
            'auto_invoice_attempts': order.auto_invoice_attempts + 1,
            'auto_invoice_last_error': str(exc)[:500],
        })
        if transaction_broken:
            self.env.cr.commit()

    @api.model
    def _auto_invoice_payment_methods(self, order):
        """Métodos de pago con los que se cobró realmente el pedido.

        Los pagos marcados is_change son el vuelto (un movimiento negativo en
        efectivo), no una forma de cobro: incluirlos mandaría al diario de
        "efectivo" cualquier pedido pagado con tarjeta que haya recibido
        vuelto.
        """
        return order.payment_ids.filtered(
            lambda p: not p.is_change).payment_method_id

    @api.model
    def _auto_invoice_split_by_payment_method(self, candidates, config):
        """Reparte los pedidos entre los dos diarios según cómo se pagaron.

        Devuelve (afip, secundario, sin_asignar). Un pedido con pagos mixtos
        (parte tarjeta AFIP, parte efectivo) se resuelve por el método que más
        monto aportó: es determinista y respeta la intención de la
        configuración sin partir la factura en dos.
        """
        afip_methods = config.auto_invoice_afip_payment_method_ids
        secondary_methods = config.auto_invoice_secondary_payment_method_ids
        afip, secondary, unmapped = [], [], []
        for order in candidates:
            totals = {'afip': 0.0, 'secondary': 0.0}
            for payment in order.payment_ids:
                if payment.is_change:
                    continue
                method = payment.payment_method_id
                if method in afip_methods:
                    totals['afip'] += payment.amount
                elif method in secondary_methods:
                    totals['secondary'] += payment.amount
            if totals['afip'] <= 0.0 and totals['secondary'] <= 0.0:
                unmapped.append(order)
            elif totals['afip'] >= totals['secondary']:
                afip.append(order)
            else:
                secondary.append(order)
        return afip, secondary, unmapped

    @api.model
    def _auto_invoice_greedy_select(self, candidates, amount_limit, count_limit, rounding):
        """Selección aleatoria greedy: factura pedidos hasta llegar a
        amount_limit sin superarlo, con tope de cantidad."""
        selected = []
        accumulated = 0.0
        for order in candidates:
            if len(selected) >= count_limit:
                break
            if amount_limit is not None and accumulated + order.amount_total > amount_limit + rounding:
                continue
            selected.append(order)
            accumulated += order.amount_total
        return selected, accumulated

    @api.model
    def _auto_invoice_for_config(self, config, dry_run=False):
        """Facturación automática diaria en dos fases:

        - Fase 1 (AFIP): factura pedidos-recibo en el diario conectado a AFIP
          hasta cubrir el % configurado del total del día (descontando lo ya
          facturado en ese diario, manual o automático).
        - Fase 2 (resto): factura todos los pedidos-recibo restantes en el
          diario secundario (sin conexión AFIP).

        Si la verificación AFIP falla (servicio caído o desfasaje de
        numeración) la fase 1 no se ejecuta. La fase 2 depende de
        auto_invoice_secondary_on_afip_block: el diario secundario no toca
        AFIP, así que puede seguir facturando, pero por defecto se frena para
        conservar pedidos con los que cumplir el % en la próxima corrida.

        Protecciones AFIP (fase 1): pre-check de numeración por tipo de
        documento, commit por factura, freno de la corrida al primer error.

        Retorna el registro de log creado.
        """
        currency = config.currency_id
        rounding = currency.rounding or 0.01
        today, day_start, day_end = self._get_auto_invoice_day_bounds(config)

        afip_journal = config.auto_invoice_journal_id or config.invoice_journal_id
        secondary_journal = config.auto_invoice_secondary_journal_id
        is_afip_ws = self._is_afip_ws_journal(afip_journal)
        max_attempts = max(1, config.auto_invoice_max_attempts or 3)

        afip_message = ''
        afip_blocked = False
        if is_afip_ws:
            afip_ok, afip_message = self._auto_invoice_check_afip_numbering(afip_journal)
            afip_blocked = not afip_ok

        day_orders = self.search([
            ('config_id', '=', config.id),
            ('state', 'in', ('paid', 'done')),
            ('date_order', '>=', day_start),
            ('date_order', '<=', day_end),
        ])
        total_day = sum(day_orders.mapped('amount_total'))
        already_invoiced = sum(
            day_orders.filtered('account_move').mapped('amount_total'))
        afip_done = sum(day_orders.filtered(
            lambda o: o.account_move and o.account_move.journal_id == afip_journal
        ).mapped('amount_total'))
        if config.auto_invoice_mode == 'payment_method':
            # Sin porcentaje: el log guarda 0 como objetivo para no sugerir
            # una meta que esta modalidad no persigue.
            target = 0.0
            afip_remaining = 0.0
        else:
            target = total_day * (config.auto_invoice_percent or 0.0) / 100.0
            afip_remaining = target - afip_done

        pending = day_orders.filtered(
            lambda o: o.state == 'paid' and not o.account_move and o.amount_total > 0)
        skipped_retries = pending.filtered(
            lambda o: o.auto_invoice_attempts >= max_attempts)
        candidates = list(pending - skipped_retries)
        # Semilla estable por config/día/avance: la selección sigue siendo
        # impredecible desde afuera, pero una simulación y la corrida real que
        # la sigue eligen los mismos pedidos, que es lo que hace útil al botón
        # "Simular".
        random.Random(
            '%s-%s-%s-%s' % (config.id, today.isoformat(), len(pending), afip_done)
        ).shuffle(candidates)

        max_per_run = max(1, config.auto_invoice_max_per_run or 10)
        run_secondary = bool(secondary_journal) and (
            not afip_blocked or config.auto_invoice_secondary_on_afip_block)
        by_payment_method = config.auto_invoice_mode == 'payment_method'
        afip_selected = []
        afip_would_amount = 0.0
        secondary_selected = []
        secondary_would_amount = 0.0
        unmapped_orders = []

        if by_payment_method:
            # El diario lo define el método de pago: no hay porcentaje ni
            # monto objetivo, solo el tope de facturas por corrida.
            afip_candidates, secondary_candidates, unmapped_orders = (
                self._auto_invoice_split_by_payment_method(candidates, config))
            if config.auto_invoice_unmapped_to_secondary:
                secondary_candidates = secondary_candidates + unmapped_orders
                unmapped_orders = []
            if not afip_blocked:
                afip_selected, afip_would_amount = self._auto_invoice_greedy_select(
                    afip_candidates, None, max_per_run, rounding)
            if run_secondary:
                secondary_selected, secondary_would_amount = self._auto_invoice_greedy_select(
                    secondary_candidates, None,
                    max_per_run * SECONDARY_MAX_FACTOR, rounding)
        else:
            if not afip_blocked and afip_remaining > rounding:
                afip_selected, afip_would_amount = self._auto_invoice_greedy_select(
                    candidates, afip_remaining, max_per_run, rounding)
            if run_secondary:
                afip_selected_ids = {o.id for o in afip_selected}
                rest = [o for o in candidates if o.id not in afip_selected_ids]
                secondary_selected, secondary_would_amount = self._auto_invoice_greedy_select(
                    rest, None, max_per_run * SECONDARY_MAX_FACTOR, rounding)

        errors = []
        afip_amount = 0.0
        afip_count = 0
        afip_failed = False
        secondary_amount = 0.0
        secondary_count = 0

        if not dry_run:
            if afip_selected and not afip_journal:
                errors.append(_(
                    'No hay diario de facturación configurado para %s.', config.name))
            elif afip_selected:
                for order in afip_selected:
                    try:
                        if is_afip_ws:
                            self._auto_invoice_one(order, config, afip_journal)
                            self.env.cr.commit()
                        else:
                            with self.env.cr.savepoint():
                                self._auto_invoice_one(order, config, afip_journal)
                        afip_amount += order.amount_total
                        afip_count += 1
                    except Exception as exc:
                        _logger.exception(
                            'POS auto invoice: error facturando %s', order.pos_reference)
                        errors.append('%s: %s' % (order.pos_reference or order.name, exc))
                        self._auto_invoice_mark_error(
                            order, exc, transaction_broken=is_afip_ws)
                        afip_failed = True
                        if is_afip_ws:
                            errors.append(_(
                                'Se detuvo la ejecución para evitar desfasajes con AFIP.'))
                        break

            # Si la fase AFIP falló, no facturar el resto: esos pedidos deben
            # quedar disponibles para cumplir el % en la próxima corrida.
            if secondary_selected and not afip_failed:
                for order in secondary_selected:
                    try:
                        with self.env.cr.savepoint():
                            self._auto_invoice_one(order, config, secondary_journal)
                        secondary_amount += order.amount_total
                        secondary_count += 1
                    except Exception as exc:
                        _logger.exception(
                            'POS auto invoice: error facturando %s (diario secundario)',
                            order.pos_reference)
                        errors.append('%s: %s' % (order.pos_reference or order.name, exc))
                        self._auto_invoice_mark_error(
                            order, exc, transaction_broken=False)

        if unmapped_orders:
            errors.insert(0, _(
                'Pedidos cuyo método de pago no está asignado a ningún diario '
                '(quedan como recibo): %s'
            ) % ', '.join(
                o.pos_reference or o.name for o in unmapped_orders))

        if skipped_retries:
            errors.insert(0, _(
                'Pedidos que superaron %s intentos y requieren revisión manual: %s'
            ) % (max_attempts, ', '.join(
                o.pos_reference or o.name for o in skipped_retries)))

        log_vals = {
            'name': '%s - %s%s' % (
                config.name,
                today.strftime('%d/%m/%Y'),
                _(' (simulación)') if dry_run else ''),
            'config_id': config.id,
            'date': today,
            'is_simulation': dry_run,
            'total_day': total_day,
            'mode': config.auto_invoice_mode,
            'percent': config.auto_invoice_percent,
            'target_amount': target,
            'already_invoiced': already_invoiced,
            'afip_already_invoiced': afip_done,
            'invoiced_amount': afip_would_amount if dry_run else afip_amount,
            'order_count': len(afip_selected) if dry_run else afip_count,
            'secondary_invoiced_amount': (
                secondary_would_amount if dry_run else secondary_amount),
            'secondary_order_count': (
                len(secondary_selected) if dry_run else secondary_count),
            'error_count': len(errors),
            'error_text': '\n'.join(str(e) for e in errors),
            'selected_orders': ', '.join(
                o.pos_reference or o.name for o in afip_selected),
            'secondary_selected_orders': ', '.join(
                o.pos_reference or o.name for o in secondary_selected),
            'afip_message': afip_message,
            'afip_blocked': afip_blocked,
        }
        log = self.env['pos.auto.invoice.log'].create(log_vals)
        if not dry_run:
            # Las facturas AFIP se commitean una por una; el log es el único
            # registro de cuánto se facturó, así que se persiste con la misma
            # disciplina en vez de depender del commit implícito del cron.
            config._auto_invoice_touch_last_run()
            self.env.cr.commit()
        return log
