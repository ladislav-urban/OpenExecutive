// What this app calls itself on its own pages, top level (OE_PRODUCT_NAME):
// the browser tab's title, the sign-in page and a card's own page. An install
// that runs inside a host's product can use that product's name, so the few
// pages someone sees outside the host's frame read as the same product. In a
// frame, the host's own name (lib/embed.ts, `productName`) is used instead.
//
// No imports, so `npm test` can exercise this.

export const DEFAULT_PRODUCT_NAME = "Open Executive";

const MAX_PRODUCT_NAME = 40;
// Plain text: no control characters and nothing that reads as markup.
const PLAIN_RE = /^[^\u0000-\u001f\u007f<>&"]+$/;

/** OE_PRODUCT_NAME as a name to show, or the default when unset or unusable. */
export function productName(raw: string | null | undefined): string {
  const name = (raw ?? "").trim().replace(/\s+/g, " ");
  if (!name || name.length > MAX_PRODUCT_NAME || !PLAIN_RE.test(name)) return DEFAULT_PRODUCT_NAME;
  return name;
}

// Where copy names the product: "{product} is answering…", "Pause {product}".
// Data and shared copy write the token, and what renders it writes the name in
// (withProduct, or useNamed in a component). Never "the Executive".
export const PRODUCT = "{product}";

/** `text` with the product's name for each PRODUCT token. */
export function withProduct(text: string, product: string): string {
  return text.split(PRODUCT).join(product);
}
