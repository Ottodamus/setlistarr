// Drag-to-reorder for draft tables: SortableJS moves the rows, then we fire "end" on the
// enclosing form, which htmx posts (hx-trigger="end") with the hidden key inputs in their new order.
htmx.onLoad(function (root) {
  root.querySelectorAll(".sortable-body").forEach(function (body) {
    if (body._sortable) return;
    body._sortable = new Sortable(body, {
      handle: ".handle",
      animation: 150,
      ghostClass: "drag-ghost",
      onEnd: function (evt) {
        if (evt.oldIndex !== evt.newIndex) body.closest("form").dispatchEvent(new Event("end"));
      },
    });
  });
});
