# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDeliveryLogisticsExpeditionFlow(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.Partner = cls.env["res.partner"]
        cls.User = cls.env["res.users"]
        cls.Product = cls.env["product.product"]
        cls.SaleOrder = cls.env["sale.order"]
        cls.StockPicking = cls.env["stock.picking"]
        cls.Expedition = cls.env["delivery.expedition"]
        cls.ExpeditionLine = cls.env["delivery.expedition.line"]

        cls.customer = cls.Partner.create({"name": "Test Customer"})

        group_user = cls.env.ref("base.group_user")
        cls.driver_a = cls.User.with_context(no_reset_password=True).create(
            {
                "name": "Driver A",
                "login": "driver_a",
                "email": "driver_a@example.com",
            }
        )

        cls.driver_b = cls.User.with_context(no_reset_password=True).create(
            {
                "name": "Driver B",
                "login": "driver_b",
                "email": "driver_b@example.com",
            }
        )

        cls.product = cls.env["product.product"].create(
            {
                "name": "Test Storable Product",
                "detailed_type": "product",  # <-- важно: storable
            }
        )

        stock_location = cls.env.ref("stock.stock_location_stock")
        cls.env["stock.quant"]._update_available_quantity(
            cls.product, stock_location, 10.0
        )

        cls.delivery_date = "2026-02-10"  # YYYY-MM-DD
        cls.priority = "urgent"

    def _create_sale_order(self, driver, delivery_date=None, priority=None):
        so = self.SaleOrder.create(
            {
                "partner_id": self.customer.id,
                "delivery_driver_id": driver.id,
                "delivery_date": delivery_date or self.delivery_date,
                "delivery_priority": priority or self.priority,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "name": self.product.name,
                            "product_id": self.product.id,
                            "product_uom_qty": 1.0,
                            "price_unit": 10.0,
                        },
                    )
                ],
            }
        )
        return so

    def _confirm_so(self, so):
        so.action_confirm()
        self.assertTrue(
            so.picking_ids,
            "Confirming the sale order should create at least one picking.",
        )
        return so.picking_ids.filtered(lambda p: p.picking_type_id.code == "outgoing")

    def test_so_confirm_creates_single_expedition_and_line(self):
        so = self._create_sale_order(self.driver_a)
        outgoing = self._confirm_so(so)

        lines = self.ExpeditionLine.search([("picking_id", "in", outgoing.ids)])
        self.assertEqual(
            len(lines),
            len(outgoing),
            "Each outgoing picking should have exactly one expedition line.",
        )

        expeditions = lines.mapped("expedition_id")
        self.assertEqual(
            len(expeditions),
            1,
            "Pickings from the SO should belong to exactly one expedition.",
        )
        expedition = expeditions[0]

        if "driver_id" in expedition._fields:
            self.assertEqual(
                expedition.driver_id.id,
                self.driver_a.id,
                "Expedition driver should match SO driver.",
            )

        picking = outgoing[0]
        for fname in [
            "delivery_date",
            "delivery_driver_id",
            "delivery_priority",
        ]:
            if fname in picking._fields and fname in so._fields:
                self.assertEqual(
                    picking[fname],
                    so[fname],
                    f"Picking field {fname} should be copied from SO.",
                )

    def test_no_empty_expedition_on_reensure(self):
        """
        Regression test for the bug: calling ensure twice could create a second empty expedition.
        We call the internal ensure method twice and assert no extra expedition without lines is created.
        """
        so = self._create_sale_order(self.driver_a)
        outgoing = self._confirm_so(so)

        ensure = getattr(so, "_ensure_expedition_and_tasks_for_outgoing_pickings", None)
        self.assertTrue(
            ensure,
            "Expected method _ensure_expedition_and_tasks_for_outgoing_pickings not found.",
        )

        ensure(outgoing)
        ensure(outgoing)

        for picking in outgoing:
            cnt = self.ExpeditionLine.search_count([("picking_id", "=", picking.id)])
            self.assertEqual(
                cnt,
                1,
                "Should not create duplicate expedition lines for the same picking.",
            )

        domain = []
        if "driver_id" in self.Expedition._fields:
            domain.append(("driver_id", "=", self.driver_a.id))
        if "delivery_date" in self.Expedition._fields:
            domain.append(("delivery_date", "=", self.delivery_date))
        expeditions = (
            self.Expedition.search(domain) if domain else self.Expedition.search([])
        )
        empty = expeditions.filtered(
            lambda e: not self.ExpeditionLine.search_count(
                [("expedition_id", "=", e.id)]
            )
        )
        self.assertFalse(empty, "There must be no empty expeditions (without lines).")

    def test_sequence_autoincrement_10_20(self):
        """
        Confirm two SOs for same driver/date so they end up in the same expedition,
        then verify expedition lines sequences are 10, 20 (or at least strictly increasing).
        """
        so1 = self._create_sale_order(self.driver_a)
        out1 = self._confirm_so(so1)

        so2 = self._create_sale_order(self.driver_a)
        out2 = self._confirm_so(so2)

        pickings = out1 | out2
        lines = self.ExpeditionLine.search(
            [("picking_id", "in", pickings.ids)], order="sequence asc, id asc"
        )
        self.assertEqual(
            len(lines),
            len(pickings),
            "Each picking should have exactly one expedition line.",
        )

        seqs = lines.mapped("sequence")
        self.assertTrue(
            all(isinstance(s, int) for s in seqs), "Sequences must be integers."
        )

        self.assertEqual(sorted(seqs), seqs, "Sequences should be ordered ascending.")
        self.assertEqual(
            len(set(seqs)), len(seqs), "Sequences should be unique per expedition."
        )

        if len(seqs) >= 2:
            self.assertEqual(
                seqs[1] - seqs[0], 10, "Sequence step should be 10 (10,20,30...)."
            )

    def test_invoice_copies_logistics_fields(self):
        so = self._create_sale_order(self.driver_a)
        self._confirm_so(so)

        invoices = so._create_invoices()
        self.assertTrue(
            invoices, "Expected invoices to be created from the sale order."
        )
        inv = invoices[0]

        for fname in [
            "delivery_date",
            "delivery_driver_id",
            "delivery_priority",
        ]:
            if fname in inv._fields and fname in so._fields:
                self.assertEqual(
                    inv[fname],
                    so[fname],
                    f"Invoice field {fname} should be copied from SO.",
                )
