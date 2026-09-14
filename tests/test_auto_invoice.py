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
