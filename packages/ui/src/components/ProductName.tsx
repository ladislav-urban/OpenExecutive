"use client";

import { useProductName } from "@/components/embed/EmbedProvider";

// The product's name (OE_PRODUCT_NAME) in running text: "Ask <ProductName />
// anything". Where copy needs it as a string (a placeholder, an aria-label),
// call useProductName or useNamed instead.
export default function ProductName() {
  return <>{useProductName()}</>;
}
