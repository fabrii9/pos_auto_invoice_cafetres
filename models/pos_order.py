# -*- coding: utf-8 -*-
import logging
import random
from datetime import datetime, time
from zoneinfo import ZoneInfo

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

MAX_AUTO_INVOICE_ATTEMPTS = 3
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
            try:
                self._auto_invoice_for_config(config, dry_run=False)
            except Exception as exc:
                _logger.exception(
                    'POS auto invoice: fallo al procesar la configuración %s', config.name)
                # Registrar el fallo en el menú de Logs para que sea visible
                # en la interfaz y no quede solo en el log del servidor.
                try:
                    with self.env.cr.savepoint():
                        self.env['pos.auto.invoice.log'].create({
                            'name': '%s - %s (error)' % (
                                config.name, fields.Date.today().strftime('%d/%m/%Y')),
                            'config_id': config.id,
                            'date': fields.Date.today(),
                            'error_count': 1,
                            'error_text': '%s: %s' % (type(exc).__name__, exc),
                        })
                except Exception:
                    _logger.exception(
                        'POS auto invoice: no se pudo registrar el log de error')

    @api.model
    def _get_auto_invoice_day_bounds(self, config):
        tz_name = self.env.user.tz or config.company_id.partner_id.tz or 'UTC'
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
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
        """Devuelve (ok, mensaje). Compara la última factura posteada en Odoo
        contra el último número autorizado en AFIP para detectar desfasajes
        antes de facturar (y no agrandarlos)."""
        # l10n_latam_document_number es computado no almacenado: no se puede
        # usar en el dominio, se filtra en Python.
        last_move = self.env['account.move'].search([
            ('journal_id', '=', journal.id),
            ('state', '=', 'posted'),
            ('move_type', '=', 'out_invoice'),
            ('l10n_latam_document_type_id', '!=', False),
        ], order='id desc', limit=10).filtered('l10n_latam_document_number')[:1]
        if not last_move:
            return True, _('Sin facturas previas en el diario: AFIP define la numeración inicial.')
        doc_type = last_move.l10n_latam_document_type_id
        try:
            last_afip = journal._l10n_ar_get_afip_last_invoice_number(doc_type)
        except Exception as exc:
            return False, _(
                'No se pudo consultar AFIP (%s). Se suspende la ejecución para '
                'evitar desfasajes de numeración.') % exc
        parts = last_move._l10n_ar_get_document_number_parts(
            last_move.l10n_latam_document_number, doc_type.code)
        last_odoo = int(parts['invoice_number'])
        if last_afip > last_odoo:
            return False, _(
                'Desfasaje detectado: último número autorizado en AFIP = %s, '
                'último en Odoo = %s (%s, tipo %s). Se suspende la facturación '
                'automática hasta regularizarlo manualmente.'
            ) % (last_afip, last_odoo, last_move.name, doc_type.name)
        return True, _('Numeración OK (AFIP: %s / Odoo: %s, tipo %s).') % (
            last_afip, last_odoo, doc_type.name)

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
        rota (errores crudos de red de AFIP), la limpia antes de escribir."""
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
        numeración) no se factura nada en ninguna fase: los pedidos quedan
        como recibo y se reintenta en la próxima corrida, preservando la
        proporción del porcentaje.

        Protecciones AFIP (fase 1): pre-check de numeración, commit por
        factura, freno de la corrida al primer error.

        Retorna el registro de log creado.
        """
        Log = self.env['pos.auto.invoice.log']
        currency = config.currency_id
        rounding = currency.rounding or 0.01
        today, day_start, day_end = self._get_auto_invoice_day_bounds(config)

        afip_journal = config.auto_invoice_journal_id or config.invoice_journal_id
        secondary_journal = config.auto_invoice_secondary_journal_id
        is_afip_ws = self._is_afip_ws_journal(afip_journal)

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
        target = total_day * (config.auto_invoice_percent or 0.0) / 100.0
        afip_remaining = target - afip_done

        pending = day_orders.filtered(
            lambda o: o.state == 'paid' and not o.account_move and o.amount_total > 0)
        candidates = list(pending.filtered(
            lambda o: o.auto_invoice_attempts < MAX_AUTO_INVOICE_ATTEMPTS))
        skipped_retries = pending - self.browse([o.id for o in candidates])
        random.shuffle(candidates)

        max_per_run = max(1, config.auto_invoice_max_per_run or 10)
        afip_selected = []
        afip_would_amount = 0.0
        secondary_selected = []
        secondary_would_amount = 0.0
        if not afip_blocked:
            if afip_remaining > rounding:
                afip_selected, afip_would_amount = self._auto_invoice_greedy_select(
                    candidates, afip_remaining, max_per_run, rounding)
            if secondary_journal:
                rest = [o for o in candidates if o not in afip_selected]
                secondary_selected, secondary_would_amount = self._auto_invoice_greedy_select(
                    rest, None, max_per_run * SECONDARY_MAX_FACTOR, rounding)

        errors = []
        afip_amount = 0.0
        afip_count = 0
        afip_failed = False
        secondary_amount = 0.0
        secondary_count = 0

        if not dry_run and not afip_blocked:
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

        if skipped_retries:
            errors.insert(0, _(
                'Pedidos que superaron %s intentos y requieren revisión manual: %s'
            ) % (MAX_AUTO_INVOICE_ATTEMPTS, ', '.join(
                skipped_retries.mapped('pos_reference'))))

        return Log.create({
            'name': '%s - %s%s' % (
                config.name,
                today.strftime('%d/%m/%Y'),
                _(' (simulación)') if dry_run else ''),
            'config_id': config.id,
            'date': today,
            'is_simulation': dry_run,
            'total_day': total_day,
            'percent': config.auto_invoice_percent,
            'target_amount': target,
            'already_invoiced': already_invoiced,
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
        })
