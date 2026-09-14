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
