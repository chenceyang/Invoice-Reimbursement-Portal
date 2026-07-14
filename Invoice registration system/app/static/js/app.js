/* Vue runs directly in the browser so Flask routes and forms stay unchanged. */
document.addEventListener("DOMContentLoaded", function () {
  if (!window.Vue) {
    console.error("Vue failed to load.");
    return;
  }

  window.Vue.createApp({
    delimiters: ["[[", "]]"],
    data() {
      return {
        uploading: false,
        draggingInvoice: false,
        uploadStatus: "",
        selectedInvoiceIds: [],
        reimburseModal: { open: false, userId: "", userName: "", quota: 0, maximum: 0, amount: "" }
      };
    },
    computed: {
      remainingReimburseQuota() {
        return Math.max(this.reimburseModal.quota - Number(this.reimburseModal.amount || 0), 0).toFixed(2);
      }
    },
    methods: {
      confirmDelete(event) {
        if (!window.confirm("确定要删除这张发票吗？")) event.preventDefault();
      },
      toggleAllInvoices(event) {
        const values = Array.from(document.querySelectorAll(".invoice-check"), item => item.value);
        this.selectedInvoiceIds = event.target.checked ? values : [];
      },
      openReimburseModal(userId, userName, quota, availableAmount) {
        const normalizedQuota = Number(quota || 0);
        const maximum = Math.min(normalizedQuota, Number(availableAmount || 0));
        this.reimburseModal = {
          open: true, userId: String(userId), userName: userName || "",
          quota: normalizedQuota, maximum, amount: maximum.toFixed(2)
        };
      },
      closeReimburseModal() {
        this.reimburseModal.open = false;
      },
      previewSelectedFile(file) {
        const preview = this.$refs.invoicePreview;
        if (!preview) return;
        const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
        preview.replaceChildren();
        const element = document.createElement(isPdf ? "iframe" : "img");
        element.src = URL.createObjectURL(file);
        element.title = isPdf ? "待上传的发票 PDF 预览" : "待上传的发票预览";
        if (!isPdf) element.alt = "待上传的发票预览";
        preview.appendChild(element);
      },
      uploadSelectedFile() {
        const input = this.$refs.invoiceFileInput;
        const file = input && input.files && input.files[0];
        if (!file || this.uploading) return;
        this.uploading = true;
        this.previewSelectedFile(file);
        this.uploadStatus = `正在上传并识别：${file.name}`;
        window.setTimeout(() => this.$refs.invoiceUploadForm.submit(), 80);
      },
      handleInvoiceDrop(event) {
        this.draggingInvoice = false;
        const files = event.dataTransfer && event.dataTransfer.files;
        if (!files || !files.length || !this.$refs.invoiceFileInput) return;
        const transfer = new DataTransfer();
        transfer.items.add(files[0]);
        this.$refs.invoiceFileInput.files = transfer.files;
        this.uploadSelectedFile();
      }
    }
  }).mount("#app");
});
