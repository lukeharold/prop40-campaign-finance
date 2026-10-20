# Combine a committee's two Cal-Access web exports into one table:
#   1. "Contributions Received"                 (Form 460, Schedule A/C)
#   2. "Late and $5000+ Contributions Received"  (Form 497)
#
# The exports are tab-separated text even though they end in .xls, and the
# two use different column names, so each is cleaned to the same columns
# before they're stacked.
#
# Avoiding double counts: a late contribution dated inside a period that a
# Form 460 statement already covers also appears on that statement. Only
# late contributions dated AFTER `statement_end` (the end date of the
# committee's most recent Form 460) are kept. For the Prop 40/41/42
# committees, the latest statement runs through Sept. 19, 2026.
#
# Usage:
#   source("combine_calaccess_exports.R")
#   no40 <- combine_exports(
#     received = "~/Downloads/export (2).xls",
#     late     = "~/Downloads/export (11).xls",
#     committee = "No on Prop 40"
#   )
#   write_csv(no40, "no_on_40_combined.csv")

library(readr)
library(dplyr)
library(stringr)

# "$1,000.00" -> 1000; "($1,000.00)" -> -1000 (parentheses mean negative,
# e.g. a returned or reduced contribution).
to_amount <- function(x) {
  neg <- grepl("^\\s*\\(.*\\)\\s*$", x)
  val <- parse_number(x)
  ifelse(neg, -val, val)
}

read_export <- function(path) {
  read_tsv(path, col_types = cols(.default = col_character()),
           locale = locale(encoding = "latin1"), trim_ws = TRUE)
}

clean_received <- function(path) {
  read_export(path) |>
    transmute(
      source       = "Form 460 (contributions received)",
      name         = `NAME OF CONTRIBUTOR`,
      payment_type = `PAYMENT TYPE`,
      city         = CITY,
      state        = STATE,
      zip          = ZIP,
      contributor_id = na_if(`ID NUMBER`, ""),
      employer     = EMPLOYER,
      occupation   = OCCUPATION,
      amount       = to_amount(AMOUNT),
      date         = as.Date(`TRANSACTION DATE`, "%m/%d/%Y"),
      filed_date   = as.Date(`FILED DATE`, "%m/%d/%Y"),
      transaction  = str_squish(`TRANSACTION NUMBER`),
      report_type  = NA_character_
    )
}

clean_late <- function(path) {
  read_export(path) |>
    # "CA / 94901" -> state "CA", zip "94901"
    tidyr::separate_wider_delim(`STATE/ZIP`, delim = "/", names = c("state", "zip"),
                                too_few = "align_start", too_many = "merge") |>
    transmute(
      source       = "Form 497 (late contribution)",
      name         = `NAME OF CONTRIBUTOR`,
      payment_type = "MONETARY",
      city         = CITY,
      state        = str_trim(state),
      zip          = str_trim(zip),
      contributor_id = na_if(`ID NUMBER`, ""),
      employer     = EMPLOYER,
      occupation   = OCCUPATION,
      amount       = to_amount(AMOUNT),
      date         = as.Date(`TRANS. DATE`, "%m/%d/%Y"),
      filed_date   = as.Date(`FILED DATE`, "%m/%d/%Y"),
      transaction  = str_squish(`TRANS #`),
      report_type  = TYPE   # INITIAL or an amendment
    ) |>
    # An amended late report repeats the same contribution; keep only the
    # most recently filed version of each one.
    mutate(tran_key = str_remove(transaction, "^.*-\\s*")) |>
    group_by(name, tran_key, date) |>
    slice_max(filed_date, n = 1, with_ties = FALSE) |>
    ungroup() |>
    select(-tran_key)
}

combine_exports <- function(received, late, committee,
                            statement_end = as.Date("2026-09-19")) {
  rec <- clean_received(received)
  lat <- clean_late(late)

  dropped <- lat |> filter(date <= statement_end)
  kept    <- lat |> filter(date > statement_end)

  if (nrow(dropped) > 0) {
    message(sprintf(
      "%s: left out %d late contribution(s) totaling $%s dated on or before %s; they're already on the Form 460.",
      committee, nrow(dropped), format(sum(dropped$amount), big.mark = ","), statement_end))
  }

  # Flag anything that still looks like it's in both files (same donor,
  # amount and date), so it can be checked by hand.
  overlap <- semi_join(kept, rec, by = c("name", "amount", "date"))
  if (nrow(overlap) > 0) {
    warning(sprintf("%s: %d late contribution(s) after %s also match a Form 460 row; check them before totaling.",
                    committee, nrow(overlap), statement_end))
  }

  bind_rows(rec, kept) |>
    mutate(committee = committee, .before = 1) |>
    arrange(desc(date))
}
