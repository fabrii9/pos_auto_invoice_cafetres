# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.addons.point_of_sale.tests.common import TestPoSCommon


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceSelection(TestPoSCommon):
    """Selección greedy: no depende de la base ni de AFIP."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PosOrder = cls.env['pos.order']

    class _FakeOrder:
        def __init__(self, amount):
            self.amount_total = amount

    def _orders(self, *amounts):
        return [self._FakeOrder(a) for a in amounts]

    def test_greedy_respects_amount_limit(self):
        candidates = self._orders(100.0, 50.0, 30.0)
        selected, total = self.PosOrder._auto_invoice_greedy_select(
            candidates, amount_limit=90.0, count_limit=10, rounding=0.01)
        self.assertEqual(total, 80.0)
        self.assertEqual(len(selected), 2, 'debe saltear el de 100 y tomar 50 + 30')

    def test_greedy_respects_count_limit(self):
        candidates = self._orders(10.0, 10.0, 10.0, 10.0)
        selected, total = self.PosOrder._auto_invoice_greedy_select(
            candidates, amount_limit=None, count_limit=2, rounding=0.01)
        self.assertEqual(len(selected), 2)
        self.assertEqual(total, 20.0)

    def test_greedy_skips_oversized_but_keeps_going(self):
        """Un pedido que no entra no corta la selección: se saltea."""
        candidates = self._orders(500.0, 40.0, 500.0, 55.0)
        selected, total = self.PosOrder._auto_invoice_greedy_select(
            candidates, amount_limit=100.0, count_limit=10, rounding=0.01)
        self.assertEqual(total, 95.0)
        self.assertEqual(len(selected), 2)

    def test_greedy_allows_exact_limit_within_rounding(self):
        candidates = self._orders(100.0)
        selected, total = self.PosOrder._auto_invoice_greedy_select(
            candidates, amount_limit=100.0, count_limit=10, rounding=0.01)
        self.assertEqual(len(selected), 1, 'el monto exacto debe entrar')
        self.assertEqual(total, 100.0)

    def test_greedy_empty_candidates(self):
        selected, total = self.PosOrder._auto_invoice_greedy_select(
            [], amount_limit=100.0, count_limit=10, rounding=0.01)
        self.assertEqual(selected, [])
        self.assertEqual(total, 0.0)


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceAfipCheck(TestPoSCommon):
    """Pre-check de numeración AFIP: el camino que decide si se factura."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PosOrder = cls.env['pos.order']

    def test_no_previous_invoices_is_ok(self):
        """Sin facturas previas AFIP define la numeración: no bloquea."""
        journal = self.env['account.journal'].create({
            'name': 'Test AFIP vacío',
            'code': 'TAFV',
            'type': 'sale',
            'company_id': self.env.company.id,
        })
        ok, message = self.PosOrder._auto_invoice_check_afip_numbering(journal)
        self.assertTrue(ok)
        self.assertIn('Sin facturas previas', message)

    def _make_journal(self, name, code):
        return self.env['account.journal'].create({
            'name': name,
            'code': code,
            'type': 'sale',
            'company_id': self.env.company.id,
        })

    def _fake_move(self, doc_type_id, doc_type_name, number, move_name):
        """Stub mínimo de account.move para el pre-check, sin depender de
        que la base tenga l10n_ar instalado."""
        test = self

        class _DocType:
            id = doc_type_id
            name = doc_type_name
            code = '1'

        class _Move:
            l10n_latam_document_type_id = _DocType()
            l10n_latam_document_number = number
            name = move_name

            def _l10n_ar_get_document_number_parts(self, num, code):
                return {'invoice_number': num.split('-')[-1]}

        return _Move()

    def _check_with_moves(self, journal, moves, afip_numbers):
        """Corre el pre-check con movimientos y respuestas de AFIP simuladas."""
        def fake_search(self_model, *args, **kwargs):
            return moves

        def fake_last_number(self_journal, doc_type):
            value = afip_numbers[doc_type.id]
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(type(self.env['account.move']), 'search', fake_search), \
             patch.object(
                 type(self.env['account.journal']),
                 '_l10n_ar_get_afip_last_invoice_number',
                 fake_last_number, create=True):
            return self.PosOrder._auto_invoice_check_afip_numbering(journal)

    def test_desfasaje_in_second_document_type_is_detected(self):
        """El bug original: solo se miraba el último comprobante, así que un
        desfasaje en el otro tipo de documento pasaba inadvertido."""
        journal = self._make_journal('AFIP multi tipo', 'AFMT')
        moves = [
            self._fake_move(1, 'Factura B', '0001-00000090', 'FB/90'),
            self._fake_move(2, 'Factura A', '0001-00000010', 'FA/10'),
        ]
        # Factura B está al día; Factura A quedó 5 números atrás en Odoo.
        ok, message = self._check_with_moves(
            journal, moves, {1: 90, 2: 15})
        self.assertFalse(ok, 'debe bloquear por el desfasaje en Factura A')
        self.assertIn('Factura A', message)

    def test_all_document_types_in_sync_is_ok(self):
        journal = self._make_journal('AFIP al día', 'AFOK')
        moves = [
            self._fake_move(1, 'Factura B', '0001-00000090', 'FB/90'),
            self._fake_move(2, 'Factura A', '0001-00000010', 'FA/10'),
        ]
        ok, message = self._check_with_moves(journal, moves, {1: 90, 2: 10})
        self.assertTrue(ok)
        self.assertIn('Factura B', message)
        self.assertIn('Factura A', message)

    def test_afip_exception_blocks(self):
        """Si el WS tira error (servicio caído) se suspende la corrida."""
        journal = self._make_journal('AFIP caído', 'AFDN')
        moves = [self._fake_move(1, 'Factura B', '0001-00000090', 'FB/90')]
        ok, message = self._check_with_moves(
            journal, moves, {1: ConnectionError('timeout')})
        self.assertFalse(ok)
        self.assertIn('No se pudo consultar AFIP', message)

    def test_afip_without_previous_comprobantes_does_not_block(self):
        """El WS devuelve False/0 cuando no hay comprobantes: no hay contra
        qué comparar, pero tampoco es un desfasaje."""
        journal = self._make_journal('AFIP sin previos', 'AFSP')
        moves = [self._fake_move(1, 'Factura B', '0001-00000090', 'FB/90')]
        ok, message = self._check_with_moves(journal, moves, {1: False})
        self.assertTrue(ok)
        self.assertIn('no reporta comprobantes previos', message)

    def test_posted_moves_without_number_block(self):
        """Antes esto devolvía un falso OK de 'sin facturas previas'."""
        journal = self._make_journal('AFIP sin numerar', 'AFSN')
        moves = [self._fake_move(1, 'Factura B', '', 'FB/draft')]
        ok, message = self._check_with_moves(journal, moves, {1: 90})
        self.assertFalse(ok)
        self.assertIn('ninguna tiene número', message)

    def test_is_afip_ws_journal_without_localization_field(self):
        """En bases sin l10n_ar_edi el diario nunca se considera AFIP WS."""
        journal = self._make_journal('Test sin WS', 'TSWS')
        # Sin l10n_ar_edi el campo no existe; con él, está vacío. En ambos
        # casos el diario no debe tratarse como AFIP WS.
        self.assertFalse(self.PosOrder._is_afip_ws_journal(journal))
        self.assertFalse(self.PosOrder._is_afip_ws_journal(self.env['account.journal']))


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceScheduling(TestPoSCommon):
    """Frecuencia por punto de venta (reemplaza la reescritura del cron)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config

    def test_never_run_is_due(self):
        self.config.auto_invoice_last_run = False
        self.assertTrue(self.config._auto_invoice_is_due())

    def test_recent_run_is_not_due(self):
        self.config.auto_invoice_frequency = 2  # cada 12 horas
        self.config.auto_invoice_last_run = fields.Datetime.now()
        self.assertFalse(self.config._auto_invoice_is_due())

    def test_elapsed_interval_is_due(self):
        self.config.auto_invoice_frequency = 24  # cada hora
        self.config.auto_invoice_last_run = fields.Datetime.subtract(
            fields.Datetime.now(), hours=2)
        self.assertTrue(self.config._auto_invoice_is_due())

    def test_two_configs_keep_independent_cadence(self):
        """El bug original: una config pisaba la frecuencia de la otra."""
        other = self.config.copy({'name': 'POS B'})
        self.config.auto_invoice_frequency = 24
        other.auto_invoice_frequency = 1
        now = fields.Datetime.now()
        self.config.auto_invoice_last_run = fields.Datetime.subtract(now, hours=2)
        other.auto_invoice_last_run = fields.Datetime.subtract(now, hours=2)
        self.assertTrue(self.config._auto_invoice_is_due(), 'cada hora: le toca')
        self.assertFalse(other._auto_invoice_is_due(), 'una vez al día: no le toca')


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceConstraints(TestPoSCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.config = cls.basic_config

    def test_secondary_journal_cannot_equal_afip_journal(self):
        from odoo.exceptions import ValidationError
        journal = self.env['account.journal'].create({
            'name': 'Diario único',
            'code': 'DUNI',
            'type': 'sale',
            'company_id': self.env.company.id,
        })
        self.config.auto_invoice_journal_id = journal
        with self.assertRaises(ValidationError):
            self.config.auto_invoice_secondary_journal_id = journal


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceByPaymentMethod(TestPoSCommon):
    """Modalidad 'Por método de pago': el diario lo define cómo se pagó."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PosOrder = cls.env['pos.order']
        cls.config = cls.basic_config
        cls.method_afip = cls.env['pos.payment.method'].create({
            'name': 'Tarjeta (AFIP)',
            'company_id': cls.env.company.id,
        })
        cls.method_secondary = cls.env['pos.payment.method'].create({
            'name': 'Efectivo (interno)',
            'company_id': cls.env.company.id,
        })
        cls.method_unmapped = cls.env['pos.payment.method'].create({
            'name': 'Sin asignar',
            'company_id': cls.env.company.id,
        })
        cls.config.write({
            'auto_invoice_mode': 'payment_method',
            'auto_invoice_afip_payment_method_ids': [(6, 0, cls.method_afip.ids)],
            'auto_invoice_secondary_payment_method_ids': [
                (6, 0, cls.method_secondary.ids)],
        })

    class _FakePayment:
        def __init__(self, method, amount, is_change=False):
            self.payment_method_id = method
            self.amount = amount
            self.is_change = is_change

    class _FakeOrder:
        def __init__(self, payments, ref='REF'):
            self.payment_ids = payments
            self.pos_reference = ref
            self.name = ref
            self.amount_total = sum(
                p.amount for p in payments if not p.is_change)

    def _order(self, *payments, ref='REF'):
        return self._FakeOrder(list(payments), ref)

    def _split(self, orders):
        return self.PosOrder._auto_invoice_split_by_payment_method(
            orders, self.config)

    def test_routes_by_payment_method(self):
        afip_order = self._order(
            self._FakePayment(self.method_afip, 100.0), ref='A')
        sec_order = self._order(
            self._FakePayment(self.method_secondary, 80.0), ref='B')
        afip, secondary, unmapped = self._split([afip_order, sec_order])
        self.assertEqual([o.pos_reference for o in afip], ['A'])
        self.assertEqual([o.pos_reference for o in secondary], ['B'])
        self.assertFalse(unmapped)

    def test_unmapped_method_is_left_alone(self):
        """Sin asignar no se factura: queda como recibo, no se adivina."""
        order = self._order(
            self._FakePayment(self.method_unmapped, 50.0), ref='C')
        afip, secondary, unmapped = self._split([order])
        self.assertFalse(afip)
        self.assertFalse(secondary)
        self.assertEqual([o.pos_reference for o in unmapped], ['C'])

    def test_mixed_payment_goes_to_larger_amount(self):
        """Pago mixto: gana el método que aportó más monto."""
        order = self._order(
            self._FakePayment(self.method_afip, 30.0),
            self._FakePayment(self.method_secondary, 70.0), ref='D')
        afip, secondary, unmapped = self._split([order])
        self.assertFalse(afip)
        self.assertEqual([o.pos_reference for o in secondary], ['D'])

    def test_mixed_payment_tie_prefers_afip(self):
        """Empate: se factura contra AFIP, que es el criterio conservador."""
        order = self._order(
            self._FakePayment(self.method_afip, 50.0),
            self._FakePayment(self.method_secondary, 50.0), ref='E')
        afip, secondary, unmapped = self._split([order])
        self.assertEqual([o.pos_reference for o in afip], ['E'])
        self.assertFalse(secondary)

    def test_change_payment_is_ignored(self):
        """El vuelto en efectivo no debe arrastrar el pedido al otro diario."""
        order = self._order(
            self._FakePayment(self.method_afip, 100.0),
            self._FakePayment(self.method_secondary, -20.0, is_change=True),
            ref='F')
        afip, secondary, unmapped = self._split([order])
        self.assertEqual([o.pos_reference for o in afip], ['F'],
                         'el vuelto no es una forma de cobro')
        self.assertFalse(secondary)

    def test_partially_mapped_mixed_payment(self):
        """Un método sin asignar no invalida al que sí lo está."""
        order = self._order(
            self._FakePayment(self.method_unmapped, 90.0),
            self._FakePayment(self.method_afip, 10.0), ref='G')
        afip, secondary, unmapped = self._split([order])
        self.assertEqual([o.pos_reference for o in afip], ['G'])
        self.assertFalse(unmapped)

    def test_method_cannot_be_in_both_journals(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.config.auto_invoice_secondary_payment_method_ids = [
                (6, 0, self.method_afip.ids)]

    def test_mode_switch_keeps_percent_config(self):
        """Cambiar de modalidad no borra lo configurado en la otra."""
        self.config.auto_invoice_percent = 30.0
        self.config.auto_invoice_mode = 'payment_method'
        self.assertEqual(self.config.auto_invoice_percent, 30.0)
        self.config.auto_invoice_mode = 'percent'
        self.assertEqual(
            self.config.auto_invoice_afip_payment_method_ids, self.method_afip)


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceClosedSessions(TestPoSCommon):
    """Pedidos que pasan a 'done' al cerrar la caja sin haberse facturado."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PosOrder = cls.env['pos.order']
        cls.config = cls.basic_config

    class _FakeSession:
        def __init__(self, state):
            self.state = state

    class _FakeOrder:
        def __init__(self, state, session_state='opened', amount=100.0, move=False):
            self.state = state
            self.session_id = TestPosAutoInvoiceClosedSessions._FakeSession(session_state)
            self.amount_total = amount
            self.account_move = move

    def _pending(self, *args, **kwargs):
        return self.PosOrder._auto_invoice_is_pending(self._FakeOrder(*args, **kwargs))

    def test_paid_order_is_pending(self):
        self.assertTrue(self._pending('paid'))

    def test_done_order_of_closed_session_is_pending(self):
        """El bug: lo vendido entre la última corrida y el cierre de caja."""
        self.assertTrue(self._pending('done', session_state='closed'))

    def test_done_order_of_open_session_is_not_pending(self):
        self.assertFalse(self._pending('done', session_state='opened'))

    def test_invoiced_or_non_positive_orders_are_not_pending(self):
        self.assertFalse(self._pending('done', session_state='closed', move=True))
        self.assertFalse(self._pending('paid', amount=0.0))
        self.assertFalse(self._pending('done', session_state='closed', amount=-50.0))

    def test_catchup_domain_window(self):
        self.config.auto_invoice_catchup_days = 7
        day_start = fields.Datetime.to_datetime('2026-10-15 03:00:00')
        domain = dict(
            ((leaf[0], leaf[1]), leaf[2])
            for leaf in self.PosOrder._auto_invoice_catchup_domain(self.config, day_start))
        self.assertEqual(domain[('date_order', '>=')],
                         fields.Datetime.to_datetime('2026-10-08 03:00:00'))
        self.assertEqual(domain[('date_order', '<')], day_start)
        self.assertEqual(domain[('state', '=')], 'done')
        self.assertEqual(domain[('session_id.state', '=')], 'closed')
        self.assertEqual(domain[('config_id', '=')], self.config.id)

    def test_catchup_days_range(self):
        with self.assertRaises(Exception), self.cr.savepoint():
            self.config.auto_invoice_catchup_days = 91
            self.config.flush_recordset()


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceOnSessionClose(TestPoSCommon):
    """El cierre de caja factura los pendientes antes de pasarlos a 'done'."""

    BASE_CLOSE = ('odoo.addons.point_of_sale.models.pos_session.PosSession.'
                  'action_pos_session_closing_control')
    AUTO_INVOICE = ('odoo.addons.pos_auto_invoice_cafetres.models.pos_order.PosOrder.'
                    '_auto_invoice_for_config')

    def setUp(self):
        super().setUp()
        self.config = self.basic_config
        self.config.auto_invoice_enabled = True
        self.open_new_session()

    def test_close_invoices_session_first(self):
        calls = []

        def fake_close(session, *args, **kwargs):
            calls.append('close')
            return True

        def fake_invoice(model, config, dry_run=False, session=None, trigger='cron'):
            calls.append(('invoice', config.id, session.id, trigger, dry_run))

        with patch(self.AUTO_INVOICE, fake_invoice), patch(self.BASE_CLOSE, fake_close):
            self.pos_session.action_pos_session_closing_control()
        self.assertEqual(calls, [
            ('invoice', self.config.id, self.pos_session.id, 'session_close', False),
            'close',
        ])

    def test_close_without_auto_invoice_does_not_invoice(self):
        self.config.auto_invoice_enabled = False
        with patch(self.AUTO_INVOICE) as invoice, \
                patch(self.BASE_CLOSE, return_value=True):
            self.pos_session.action_pos_session_closing_control()
        invoice.assert_not_called()

    def test_invoice_failure_does_not_block_close(self):
        """AFIP caído al cierre: el cajero cierra igual y queda el log."""
        with patch(self.AUTO_INVOICE, side_effect=RuntimeError('AFIP caído')), \
                patch(self.BASE_CLOSE, return_value=True) as close, \
                patch.object(self.env.cr, 'rollback'), \
                patch.object(self.env.cr, 'commit'):
            self.pos_session.action_pos_session_closing_control()
        close.assert_called_once()
        log = self.env['pos.auto.invoice.log'].search(
            [('config_id', '=', self.config.id)], limit=1)
        self.assertEqual(log.trigger, 'session_close')
        self.assertIn('AFIP caído', log.error_text)


@tagged('post_install', '-at_install')
class TestPosAutoInvoiceClosingFlow(TestPoSCommon):
    """De punta a punta: pedidos reales, cierre de caja real, facturas reales."""

    def setUp(self):
        super().setUp()
        self.config = self.basic_config
        self.product = self.create_product('Café', self.categ_basic, 100.0)
        self.secondary_journal = self.env['account.journal'].create({
            'name': 'Interno',
            'code': 'INTX',
            'type': 'sale',
            'company_id': self.env.company.id,
        })
        self.config.write({
            'payment_method_ids': [(6, 0, (self.cash_pm1 | self.bank_pm1).ids)],
            'auto_invoice_mode': 'payment_method',
            'auto_invoice_journal_id': self.config.invoice_journal_id.id,
            'auto_invoice_secondary_journal_id': self.secondary_journal.id,
            'auto_invoice_afip_payment_method_ids': [(6, 0, self.bank_pm1.ids)],
            'auto_invoice_secondary_payment_method_ids': [(6, 0, self.cash_pm1.ids)],
            'auto_invoice_partner_id': self.customer.id,
        })
        # El proceso commitea por diseño (AFIP); en tests no se puede.
        commit = patch.object(self.env.cr, 'commit')
        commit.start()
        self.addCleanup(commit.stop)
        self.open_new_session()
        self.bank_order, self.cash_order = self._create_orders([
            {'pos_order_lines_ui_args': [(self.product, 1)],
             'payments': [(self.bank_pm1, 100.0)]},
            {'pos_order_lines_ui_args': [(self.product, 2)],
             'payments': [(self.cash_pm1, 200.0)]},
        ]).values()

    def _close_session(self):
        cash = sum(self.cash_order.payment_ids.mapped('amount'))
        self.pos_session.post_closing_cash_details(cash)
        self.pos_session.close_session_from_ui()
        self.assertEqual(self.pos_session.state, 'closed')

    def _assert_invoiced(self):
        self.assertEqual(self.bank_order.account_move.journal_id, self.config.invoice_journal_id)
        self.assertEqual(self.cash_order.account_move.journal_id, self.secondary_journal)
        self.assertEqual(
            set((self.bank_order | self.cash_order).account_move.mapped('state')), {'posted'})

    def test_session_close_invoices_pending_orders(self):
        self.config.auto_invoice_enabled = True
        self._close_session()
        self._assert_invoiced()
        log = self.env['pos.auto.invoice.log'].search(
            [('config_id', '=', self.config.id)], limit=1)
        self.assertEqual(log.trigger, 'session_close')
        self.assertEqual(log.session_id, self.pos_session)
        self.assertEqual(log.order_count + log.secondary_order_count, 2)

    def test_cron_recovers_orders_of_closed_session(self):
        """El caso de producción: la caja cerró sin facturar (antes de este
        cambio, o con AFIP caído) y los pedidos quedaron en 'done'."""
        self.config.auto_invoice_enabled = False
        self._close_session()
        orders = self.bank_order | self.cash_order
        self.assertEqual(set(orders.mapped('state')), {'done'})
        self.assertFalse(orders.account_move)
        # Que sean de días anteriores: solo los encuentra la recuperación.
        orders.write({'date_order': fields.Datetime.subtract(fields.Datetime.now(), days=2)})

        self.config.write({'auto_invoice_enabled': True, 'auto_invoice_catchup_days': 0})
        self.env['pos.order']._auto_invoice_for_config(self.config)
        self.assertFalse(orders.account_move, 'con 0 días no se recupera nada')

        self.config.auto_invoice_catchup_days = 7
        log = self.env['pos.order']._auto_invoice_for_config(self.config)
        self._assert_invoiced()
        self.assertEqual(log.recovered_order_count, 2)
        # Odoo saca estos pedidos del asiento de cierre con una reversión.
        reversals = self.env['account.move'].search(
            [('reversed_pos_order_id', 'in', orders.ids)])
        self.assertEqual(len(reversals), 2)
        self.assertEqual(set(reversals.mapped('state')), {'posted'})
