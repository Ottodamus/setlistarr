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

// Cover upload: pick a file -> crop to a square with Cropper.js (live preview) -> upload the
// cropped 1024x1024 JPEG. The server re-checks and normalizes whatever it receives.
document.addEventListener("change", function (e) {
  if (!e.target.classList || !e.target.classList.contains("cover-file")) return;
  var widget = e.target.closest(".cover-widget");
  var file = e.target.files && e.target.files[0];
  if (!widget || !file) return;
  var crop = widget.querySelector(".cover-crop");
  var img = widget.querySelector(".crop-src");
  if (widget._cropper) { widget._cropper.destroy(); widget._cropper = null; }
  if (widget._objectUrl) URL.revokeObjectURL(widget._objectUrl);
  widget._objectUrl = URL.createObjectURL(file);
  img.onload = function () {
    crop.hidden = false;
    widget._cropper = new Cropper(img, {
      aspectRatio: 1, viewMode: 1, autoCropArea: 1, background: false,
      preview: widget.querySelector(".cover-preview"),
    });
  };
  img.src = widget._objectUrl;
});

document.addEventListener("click", function (e) {
  var widget = e.target.closest && e.target.closest(".cover-widget");
  if (!widget) return;
  if (e.target.classList.contains("cover-cancel")) {
    if (widget._cropper) { widget._cropper.destroy(); widget._cropper = null; }
    widget.querySelector(".cover-crop").hidden = true;
    widget.querySelector(".cover-file").value = "";
  }
  if (e.target.classList.contains("cover-use") && widget._cropper) {
    var button = e.target;
    button.disabled = true;
    button.textContent = "Uploading…";
    widget._cropper.getCroppedCanvas({ width: 1024, height: 1024, imageSmoothingQuality: "high", fillColor: "#000" })
      .toBlob(function (blob) {
        var form = new FormData();
        form.append("file", blob, "cover.jpg");
        form.append(widget.dataset.ownerField, widget.dataset.ownerId);
        fetch("/covers/upload", { method: "POST", body: form, headers: { "HX-Request": "true" } })
          .then(function (r) { return r.text(); })
          .then(function (html) {
            var tmp = document.createElement("div");
            tmp.innerHTML = html.trim();
            var fresh = tmp.firstElementChild;
            widget.replaceWith(fresh);
            htmx.process(fresh);
          })
          .catch(function () { button.disabled = false; button.textContent = "Use this crop"; alert("Upload failed"); });
      }, "image/jpeg", 0.92);
  }
});
