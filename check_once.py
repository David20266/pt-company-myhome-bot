        )

    new_items: list[dict[str, Any]] = []

    if not initialized:
        state["initialized"] = True
        send_telegram(
            "✅ მონიტორინგი ჩართულია.\n\n"
            "ვაკონტროლებ MyHome.ge-ზე თბილისში გასაქირავებელ ბინებს, კერძო სახლებსა და აგარაკებს — 400$-დან, "
            "მხოლოდ ფიზიკური პირების განცხადებებს.\n\n"
            "არსებული განცხადებები შენახულია; შეტყობინებები მოვა მხოლოდ ახალი განცხადებების დამატებისას."
        )
        print(
            f"Initial baseline: stored {len(unseen_items)} "
            "currently visible listings"
        )

    elif previous_scan_at is None:
        print(
            f"Freshness baseline created. Stored {len(unseen_items)} "
            "unseen listings without notifications."
        )

    else:
        await enrich_unseen_items(unseen_items)

        retry_unverified: list[dict[str, Any]] = []

        for item in unseen_items:
            if not item.get("posted_at_utc"):
                retry_unverified.append(item)
                continue

            if is_fresh_listing(
                item,
                previous_scan_at,
                run_started_at,
                recovery_mode=recovery_mode,
            ):
                new_items.append(item)

        for item in retry_unverified:
            key = item["source_key"]
            listing_id = item["listing_id"]
            state["seen"][key] = [
                seen_id
                for seen_id in state["seen"][key]
                if seen_id != listing_id
            ]
            print(
                f"RETRY {listing_id}: publication time unverified; "
                "not persisted as seen"
            )

        new_items.sort(
            key=lambda item: (
                parse_iso_datetime(item.get("posted_at_utc", ""))
                or datetime.min.replace(tzinfo=timezone.utc),
                int(item["listing_id"]),
            )
        )

        sent_count = 0
        failed_items: list[dict[str, Any]] = []

        for item in new_items:
            if send_listing_to_telegram(item):
                sent_count += 1
            else:
                failed_items.append(item)
                print(
                    f"Telegram delivery failed for {item['listing_id']}; "
                    "it will be retried on the next run"
                )

        for item in failed_items:
            key = item["source_key"]
            listing_id = item["listing_id"]
            state["seen"][key] = [
                seen_id
                for seen_id in state["seen"][key]
                if seen_id != listing_id
            ]

    if not initialized or previous_scan_at is None:
        sent_count = 0

    if recovery_mode:
        state["url_change_recovery_completed"] = True
        print("URL-change recovery completed; future runs use normal freshness.")

    state["heartbeat_week"] = run_started_at.strftime("%G-W%V")
    state["last_successful_scan_at"] = run_started_at.isoformat()
    save_state(state)

    print(
        f"Found {len(listings)} listings; unseen {len(unseen_items)}; "
        f"verified-new {len(new_items)}; sent {sent_count} notifications"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
