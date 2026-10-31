"""Purchase to pay: suppliers, purchase orders and their approval."""

# Document types this pack owns: generic writes to them are handed to the pack. Includes the
# master data the pack's checks read (Item, prices, warehouses, supplier addresses and
# contacts). Address and Contact are owned when linked to a supplier, decided at write time.
P2P_OWNED: frozenset[str] = frozenset({
    "Supplier",
    "Address",
    "Contact",
    "Item",
    "Item Price",
    "Price List",
    "Warehouse",
    "Material Request",
    "Request for Quotation",
    "Supplier Quotation",
    "Purchase Order",
    "Purchase Receipt",
    "Purchase Invoice",
    "Payment Entry",
    "ERP Guard Change Request",
})
