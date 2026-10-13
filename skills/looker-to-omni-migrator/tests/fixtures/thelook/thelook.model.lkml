# Test fixture: a small, original LookML project modelled on the public
# thelook e-commerce dataset (bigquery-public-data.thelook_ecommerce).
connection: "thelook_ecommerce"

include: "/views/*.view.lkml"

datagroup: nightly {
  sql_trigger: SELECT CURRENT_DATE() ;;
}

access_grant: can_view_pii {
  user_attribute: can_see_pii
  allowed_values: ["yes"]
}

access_grant: can_view_financials {
  user_attribute: department
  allowed_values: ["finance", "executive"]
}

explore: order_items {
  label: "Orders"
  description: "Order line items with shopper, product and inventory context"
  required_access_grants: [can_view_financials]

  access_filter: {
    field: users.country
    user_attribute: allowed_country
  }

  sql_always_where: ${order_items.status} != 'Cancelled' ;;

  always_filter: {
    filters: [order_items.created_date: "90 days"]
  }

  join: orders {
    type: left_outer
    relationship: many_to_one
    sql_on: ${order_items.order_id} = ${orders.order_id} ;;
  }

  join: users {
    type: left_outer
    relationship: many_to_one
    sql_on: ${order_items.user_id} = ${users.id} ;;
  }

  join: inventory_items {
    type: inner
    relationship: one_to_one
    sql_on: ${order_items.inventory_item_id} = ${inventory_items.id} ;;
  }

  join: products {
    type: left_outer
    relationship: many_to_one
    sql_on: ${inventory_items.product_id} = ${products.id} ;;
  }

  join: distribution_centers {
    type: left_outer
    relationship: many_to_one
    sql_on: ${products.distribution_center_id} = ${distribution_centers.id} ;;
  }
}

explore: users {
  label: "Shoppers"

  join: user_order_facts {
    relationship: one_to_one
    sql_on: ${users.id} = ${user_order_facts.user_id} ;;
  }

  join: orders {
    relationship: one_to_many
    sql_on: ${users.id} = ${orders.user_id}
      {% if orders.status._in_query %} AND ${orders.status} IS NOT NULL {% endif %} ;;
  }
}

explore: events {
  label: "Web Events"

  join: visitor {
    from: users
    type: left_outer
    relationship: many_to_one
    sql_on: ${events.user_id} = ${visitor.id} ;;
  }
}
