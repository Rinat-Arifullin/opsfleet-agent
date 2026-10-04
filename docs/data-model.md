# Data model: `bigquery-public-data.thelook_ecommerce`

Checked via `INFORMATION_SCHEMA` and counts on 2026-10-04. The agent uses only these four tables.

```mermaid
erDiagram
    users ||--o{ orders : places
    users ||--o{ order_items : "buys (denormalised user_id)"
    orders ||--|{ order_items : contains
    products ||--o{ order_items : "sold as"

    users {
        INT64 id PK
        STRING first_name "PII"
        STRING last_name "PII"
        STRING email "PII"
        STRING street_address "PII"
        STRING postal_code "PII"
        FLOAT64 latitude "PII"
        FLOAT64 longitude "PII"
        GEOGRAPHY user_geom "PII"
        INT64 age
        STRING gender
        STRING city
        STRING state
        STRING country
        STRING traffic_source
        TIMESTAMP created_at
    }
    orders {
        INT64 order_id PK
        INT64 user_id FK
        STRING status
        STRING gender
        TIMESTAMP created_at
        TIMESTAMP shipped_at
        TIMESTAMP delivered_at
        TIMESTAMP returned_at
        INT64 num_of_item
    }
    order_items {
        INT64 id PK
        INT64 order_id FK
        INT64 user_id FK
        INT64 product_id FK
        INT64 inventory_item_id
        STRING status
        FLOAT64 sale_price
        TIMESTAMP created_at
        TIMESTAMP shipped_at
        TIMESTAMP delivered_at
        TIMESTAMP returned_at
    }
    products {
        INT64 id PK
        STRING name
        STRING brand
        STRING category
        STRING department
        FLOAT64 cost
        FLOAT64 retail_price
        STRING sku
        INT64 distribution_center_id
    }
```

| Table | Rows | Notes |
|---|---|---|
| `users` | 100,000 | The only table with PII |
| `orders` | 124,966 | Order header; **no product column** |
| `order_items` | 181,033 | The only link to products; revenue = `sum(sale_price)` |
| `products` | 29,120 | 26 categories, 2 departments (Women, Men), 2,756 brands |

- **Statuses** (orders and items): Complete, Shipped, Processing, Cancelled, Returned.
- **Order dates:** 2019-01-12 .. today (the dataset is refreshed regularly, so "last month" has real data).
- **Product scope:** product data reaches orders and customers only through `order_items.product_id`. A scoped query therefore always goes `order_items ⋈ products` filtered by the user's scope (a brand list or the CEO `all` flag), and customer and order metrics are computed from those in-scope items.
- **Categories:** Accessories, Active, Blazers & Jackets, Clothing Sets, Dresses, Fashion Hoodies & Sweatshirts, Intimates, Jeans, Jumpsuits & Rompers, Leggings, Maternity, Outerwear & Coats, Pants, Pants & Capris, Plus, Shorts, Skirts, Sleep & Lounge, Socks, Socks & Hosiery, Suits, Suits & Sport Coats, Sweaters, Swim, Tops & Tees, Underwear.
