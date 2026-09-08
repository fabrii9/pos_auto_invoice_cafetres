import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";

// Pedidos nuevos siempre arrancan como recibo (to_invoice = false).
// El default nativo ya es false; esto protege contra otros módulos
// que lo activen en setup (y contra el auto-tilde por cliente empresa).
patch(PosOrder.prototype, {
    setup(_defaultObj, options) {
        super.setup(...arguments);
        if (!_defaultObj?.id) {
            this.to_invoice = false;
        }
    },
    setPartner(partner) {
        super.setPartner(...arguments);
        this.to_invoice = false;
    },
});

// l10n_ar_pos fuerza setToInvoice(true) al entrar a la pantalla de pago
// en compañías argentinas. Este patch se aplica después (dependencia en
// el manifest) y lo deja destildado; el cajero puede tildarlo a mano.
patch(PaymentScreen.prototype, {
    onMounted() {
        super.onMounted(...arguments);
        this.currentOrder.setToInvoice(false);
    },
});
