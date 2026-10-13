explore: order_items {
  label: "Order Items"
  description: "Sales by item, with the customer who placed each order"

  # Rows are limited by a user attribute. The migrator carries this
  # into the topic or flags it as MANUAL ACTION REQUIRED in the report.
  access_filter: {
    field: users.country
    user_attribute: allowed_country
  }

  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${order_items.user_id} = ${users.id} ;;
  }
}

explore: users {
  label: "Customers"
}
