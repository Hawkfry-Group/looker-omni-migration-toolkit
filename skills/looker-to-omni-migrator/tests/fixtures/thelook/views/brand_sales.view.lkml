# Native derived table: completed sales per brand.
view: brand_sales {
  derived_table: {
    explore_source: order_items {
      column: brand { field: products.brand }
      column: total_sales { field: order_items.total_sale_price }
      filters: [order_items.status: "Complete"]
    }
  }

  dimension: brand {
    primary_key: yes
    type: string
    sql: ${TABLE}.brand ;;
  }

  dimension: total_sales {
    type: number
    sql: ${TABLE}.total_sales ;;
    value_format_name: usd
  }

  measure: average_brand_sales {
    type: average
    sql: ${total_sales} ;;
    value_format_name: usd
  }
}
