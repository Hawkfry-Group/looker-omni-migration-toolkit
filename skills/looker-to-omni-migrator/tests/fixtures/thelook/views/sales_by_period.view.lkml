# Sales per customer over a period the user picks. SQL derived table whose
# Liquid has an exact Omni Mustache equivalent.
view: sales_by_period {
  derived_table: {
    sql:
      SELECT
        order_items.user_id AS user_id,
        COUNT(*) AS order_count,
        SUM(order_items.sale_price) AS revenue,
        SUM(order_items.cost) AS cost
      FROM thelook_ecommerce.order_items AS order_items
      WHERE {% condition order_date %} order_items.created_at {% endcondition %}
        AND {% condition status_pick %} order_items.status {% endcondition %}
        AND order_items.created_at < {% date_end order_date %}
        AND order_items.sale_price >= {% parameter min_sale_price %}
        AND order_items.region = '{{ _user_attributes['sales_region'] }}'
      GROUP BY 1 ;;
  }

  filter: order_date {
    type: date
    label: "Order Date"
    description: "Period the sales are counted over"
  }

  filter: status_pick {
    type: string
    suggest_dimension: order_items.status
    default_value: "Complete"
  }

  parameter: min_sale_price {
    type: number
    default_value: "0"
  }

  parameter: metric {
    type: unquoted
    label: "Metric"
    allowed_value: { label: "Revenue" value: "revenue" }
    allowed_value: { label: "Cost" value: "cost" }
    default_value: "revenue"
  }

  parameter: unused_choice {
    type: string
    allowed_value: { value: "a" }
  }

  dimension: user_id {
    primary_key: yes
    type: number
    sql: ${TABLE}.user_id ;;
  }

  dimension: order_count {
    type: number
    sql: ${TABLE}.order_count ;;
  }

  dimension: selected_metric {
    type: number
    sql: ${TABLE}.{% parameter metric %} ;;
  }

  dimension: metric_label {
    type: string
    sql: {% if metric._parameter_value == 'cost' %} 'Cost' {% else %} 'Revenue' {% endif %} ;;
  }

  measure: total_selected_metric {
    type: sum
    sql: ${selected_metric} ;;
  }
}
