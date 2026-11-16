# Test fixture: explore inheritance (extends), a base explore that only
# exists to be extended, and an always_filter that can be translated.

explore: inventory_base {
  extension: required
  view_name: inventory_items
  group_label: "Inventory"

  always_filter: {
    filters: [inventory_items.sold_date: "30 days ago for 30 days"]
  }

  join: products {
    type: left_outer
    relationship: many_to_one
    sql_on: ${inventory_items.product_id} = ${products.id} ;;
  }
}

explore: inventory {
  extends: [inventory_base]
  label: "Inventory"

  join: distribution_centers {
    type: left_outer
    relationship: many_to_one
    sql_on: ${products.distribution_center_id} = ${distribution_centers.id} ;;
  }
}
