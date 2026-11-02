# Supplier Invoices That Don't Match, or Arrive Twice

Most supplier invoices are right. The few that aren't cost money quietly: an invoice for more than was received, a price above the one agreed, an invoice keyed without its order so nothing was checked, and the same invoice entered twice and paid twice. These are found by hand at the month-end close, by the supplier's statement, or not at all.

Checking for them is routine work: the records either agree or they don't. Deciding what to do about one that doesn't is a person's job.

## The Three-Way Match

A supplier invoice is checked against two other documents:

- the **purchase order**: what was ordered, at what price and terms;
- the **goods receipt** (or the delivery note): what actually arrived, and how many.

For each line, the invoice should bill no more than was received, at no more than the agreed price. Small differences within an agreed tolerance (a rounding difference, a small freight charge the order allows) are normal. Anything beyond it is an exception to look at.

## Where Exceptions Hide

- **Billed for more than was received.** The invoice quantity is above the receipt's, often because part of the order hasn't arrived yet.
- **Billed at a higher price.** The invoice's rate is above the order's: a list price used instead of the agreed one, or a price rise nobody agreed.
- **Keyed without its order.** The invoice was entered on its own, not from the purchase order, so neither quantity nor price was checked against anything. Every unlinked invoice from a supplier that normally works from orders is worth a look.
- **Charges the order doesn't have.** Freight, handling or surcharges not on the order.
- **The same invoice twice.** Entered twice, sometimes under different internal numbers, sometimes with the supplier's invoice number mistyped the second time.

## Finding Duplicates

An exact repeat of the supplier's invoice number is easy to catch, and many systems refuse it. The real cases are the near ones:

- **Same supplier, same amount, close dates** (within a few weeks), whatever the invoice numbers say.
- **Same supplier and an invoice number that differs by a typo:** two digits swapped, one digit off, a missing prefix or a trailing space.
- **Same supplier, same order or receipt** billed by two invoices that together come to more than was received.

Group the period's invoices by supplier, then compare amounts, dates and invoice numbers within each group. A split delivery billed in two parts is not a duplicate. Before calling a pair a duplicate, show both side by side (numbers, dates, amounts, lines, the order and receipt each refers to) so a person can see why.

## How to Check

1. **Fix the period.** For example, the last three months. Say which.
2. **List the supplier invoices** in it, with each one's supplier, invoice number, date, amount, whether it is paid, and the order and receipt it refers to.
3. **Match each linked invoice** to its order and receipt, line by line: quantity billed against received, rate against ordered.
4. **List the unlinked ones** and match them by hand where an order exists.
5. **Look for duplicates** as above.
6. **Report each exception** with the figures that show it, the amount at stake, whether it has been paid, and the documents it comes from, with the company and currency.

Every figure comes from the books, and the answer says where each one came from so a person can open it and check.

## Who Acts

- **An unpaid exception:** whoever approves supplier invoices, or the buyer who placed the order, holds it and asks the supplier for a credit note or a corrected invoice. Asking is routine.
- **A paid duplicate or overcharge:** whoever runs payables asks the supplier for a refund or sets it against the next invoice.
- **Whether to pay, hold or dispute, and how far to take it,** is a decision about money: name who decides (usually whoever approves payments), with the options and what each means in the figures.

## What It Is Not

- It doesn't post, pay, hold or cancel anything. A person does that, in the system that holds the books.
- A mismatch isn't proof of an error: a price change may have been agreed by phone. The answer says what the records show and who can confirm it.
