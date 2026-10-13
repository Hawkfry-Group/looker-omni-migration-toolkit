# Lifetime order totals per user, built from order_items.
view: user_order_facts {
  derived_table: {
    sql:
      SELECT
        user_id,
        COUNT(DISTINCT order_id) AS lifetime_orders,
        SUM(sale_price) AS lifetime_revenue,
        MIN(created_at) AS first_order_at
      FROM thelook_ecommerce.order_items
      GROUP BY user_id ;;
    datagroup_trigger: nightly
  }

  dimension: user_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.user_id ;;
  }

  dimension: lifetime_orders {
    type: number
    sql: ${TABLE}.lifetime_orders ;;
  }

  dimension: lifetime_revenue {
    type: number
    sql: ${TABLE}.lifetime_revenue ;;
    value_format_name: usd
  }

  dimension: is_repeat_buyer {
    type: yesno
    sql: ${lifetime_orders} > 1 ;;
  }

  dimension_group: first_order {
    type: time
    timeframes: [raw, date, month]
    sql: ${TABLE}.first_order_at ;;
  }

  measure: average_lifetime_revenue {
    type: average
    sql: ${lifetime_revenue} ;;
    value_format_name: usd
  }
}
