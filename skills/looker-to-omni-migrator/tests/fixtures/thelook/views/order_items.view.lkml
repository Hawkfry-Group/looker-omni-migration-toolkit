# Line items of the thelook sample store. One row per item sold.
view: order_items {
  sql_table_name: `thelook_ecommerce.order_items` ;;
  drill_fields: [id, order_id]

  set: detail {
    fields: [id, order_id, status, sale_price]
  }

  dimension: id {
    primary_key: yes
    type: number
    sql: ${TABLE}.id ;;
  }

  dimension: order_id {
    type: number
    sql: ${TABLE}.order_id ;;
  }

  dimension: user_id {
    type: number
    hidden: yes
    sql: ${TABLE}.user_id ;;
  }

  dimension: inventory_item_id {
    type: number
    hidden: yes
    sql: ${TABLE}.inventory_item_id ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
    html: <b>{{ value }}</b> ;;
  }

  dimension: sale_price {
    type: number
    sql: COALESCE(${TABLE}.sale_price, 0) ;;
    value_format_name: usd
  }

  dimension: gross_margin {
    type: number
    sql: ${sale_price} - ${inventory_items.cost} ;;
    value_format_name: usd
    required_access_grants: [can_view_financials]
  }

  dimension: is_returned {
    type: yesno
    sql: ${TABLE}.returned_at IS NOT NULL ;;
  }

  dimension: price_tier {
    type: tier
    tiers: [0, 25, 50, 100]
    style: integer
    sql: ${sale_price} ;;
  }

  dimension_group: created {
    type: time
    timeframes: [raw, date, week, month, year]
    sql: ${TABLE}.created_at ;;
  }

  dimension_group: fulfillment {
    type: duration
    intervals: [day]
    sql_start: ${TABLE}.created_at ;;
    sql_end: ${TABLE}.delivered_at ;;
  }

  measure: count {
    type: count
    drill_fields: [detail*]
  }

  measure: order_count {
    type: count_distinct
    sql: ${order_id} ;;
    value_format_name: decimal_0
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
    value_format_name: usd_0
  }

  measure: median_sale_price {
    type: median
    sql: ${sale_price} ;;
  }

  measure: total_gross_margin {
    type: sum
    sql: ${gross_margin} ;;
    value_format_name: usd
  }

  measure: gross_margin_per_order {
    type: number
    sql: ${total_gross_margin} / NULLIF(${order_count}, 0) ;;
    value_format_name: usd
  }

  measure: returned_count {
    type: count
    filters: [status: "Returned"]
  }

  measure: completed_or_shipped_count {
    type: count
    filters: [status: "Complete, Shipped"]
  }

  measure: large_order_count {
    type: count_distinct
    sql: ${order_id} ;;
    filters: [sale_price: ">=100"]
  }

  measure: wildcard_status_count {
    type: count
    filters: [status: "%Ship%"]
  }

  measure: running_total_sales {
    type: running_total
    sql: ${total_sale_price} ;;
  }

  measure: total_sales_eur {
    type: sum
    sql: ${sale_price} ;;
    value_format: "€#,##0.00"
  }
}
