# Line items for the thelook e-commerce sample store. One row per item sold.
view: order_items {
  sql_table_name: thelook_ecommerce.order_items ;;
  label: "Order Items"
  description: "One row per item sold. Each order contains one or more items."

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
    description: "Unique identifier for the order item"
  }

  dimension: order_id {
    type: number
    sql: ${TABLE}.order_id ;;
    description: "Order this item belongs to"
  }

  dimension: user_id {
    type: number
    hidden: yes
    sql: ${TABLE}.user_id ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
    description: "Fulfilment status of the item, for example Shipped or Returned"
  }

  dimension: sale_price {
    type: number
    sql: ${TABLE}.sale_price ;;
    value_format_name: usd
    description: "Price the item sold for"
  }

  dimension: is_returned {
    type: yesno
    sql: ${TABLE}.returned_at IS NOT NULL ;;
    label: "Is Returned"
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month, year]
    sql: ${TABLE}.created_at ;;
    description: "When the item was ordered"
  }

  measure: count {
    type: count
    drill_fields: [id, order_id, status, sale_price]
  }

  measure: order_count {
    type: count_distinct
    sql: ${order_id} ;;
    value_format_name: decimal_0
    description: "Number of distinct orders"
  }

  measure: total_sale_price {
    type: sum
    sql: ${sale_price} ;;
    value_format_name: usd
    label: "Total Sales"
  }

  measure: average_sale_price {
    type: average
    sql: ${sale_price} ;;
    value_format_name: usd
  }
}
