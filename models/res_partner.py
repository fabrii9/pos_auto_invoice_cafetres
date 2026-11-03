# -*- coding: utf-8 -*-
from odoo import _, api, models
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.constrains(
        'vat', 'l10n_latam_identification_type_id',
        'l10n_ar_afip_responsibility_type_id', 'parent_id')
    def _check_l10n_ar_responsibility_with_vat(self):
        """Un contacto con CUIT/DNI y sin responsabilidad ARCA no se puede
        facturar: l10n_ar no sabe si corresponde Factura A o B y la
        validación falla ("el diario requiere un tipo de documento").

        El formulario ya lo exige; esta validación cubre lo que no pasa por
        él (importaciones, API). Los contactos hijos heredan el dato del
        contacto comercial, así que solo se controla el padre.
        """
        for partner in self:
            if partner.parent_id or not partner.vat:
                continue
            if not partner.l10n_latam_identification_type_id.l10n_ar_afip_code:
                continue
            if not partner.l10n_ar_afip_responsibility_type_id:
                raise ValidationError(_(
                    'El contacto "%s" tiene número de identificación pero no '
                    'tiene cargado el Tipo de Responsabilidad ARCA. Sin ese dato '
                    'no se le puede emitir factura (A o B). Cargalo antes de '
                    'guardar.', partner.display_name))
