/* Run the real upload/commit/cleanup code against a fake atomic transaction
 * sink. No DRM device, modeset, or display access occurs in this test. */
#define drmModeAtomicAlloc test_atomic_alloc
#define drmModeAtomicAddProperty test_atomic_add
#define drmModeAtomicCommit test_atomic_commit
#define drmModeAtomicFree test_atomic_free
#define drmModeRmFB test_remove_fb
#include "../../native/kms_plane.c"
#include <assert.h>

typedef struct { uint64_t values[NPROPS]; } FakeRequest;
static uint32_t scanout_fb, presented[8];
static unsigned present_count, removed;
static bool reject_commit;

drmModeAtomicReq *test_atomic_alloc(void) {
    return (drmModeAtomicReq *)calloc(1, sizeof(FakeRequest));
}

int test_atomic_add(drmModeAtomicReq *request, uint32_t object, uint32_t property, uint64_t value) {
    assert(object == 9); /* only the chosen overlay plane */
    assert(property >= 1 && property <= NPROPS);
    ((FakeRequest *)request)->values[property - 1] = value;
    return 0;
}

int test_atomic_commit(int fd, drmModeAtomicReq *request, uint32_t flags, void *data) {
    (void)fd;
    (void)data;
    assert(!(flags & DRM_MODE_ATOMIC_ALLOW_MODESET));
    const uint64_t *values = ((const FakeRequest *)request)->values;
    if (reject_commit) {
        errno = EINVAL;
        return -1;
    }
    if (values[0]) {
        assert(values[1] == 42);
        assert(values[2] == 0 && values[3] == 0);
        assert(values[4] == (720u << 16) && values[5] == (400u << 16));
        assert(values[6] == 0 && values[7] == 6);
        assert(values[8] == 1920 && values[9] == 1067);
    } else {
        assert(values[1] == 0); /* detach before framebuffer destruction */
    }
    if (!(flags & DRM_MODE_ATOMIC_TEST_ONLY)) {
        scanout_fb = values[0];
        presented[present_count++] = scanout_fb;
    }
    return 0;
}

void test_atomic_free(drmModeAtomicReq *request) {
    free(request);
}

int test_remove_fb(int fd, uint32_t fb) {
    (void)fd;
    assert(fb == 101 || fb == 102);
    assert(scanout_fb == 0);
    ++removed;
    return 0;
}

int main(void) {
    Context *ctx = calloc(1, sizeof(*ctx));
    assert(ctx);
    ctx->fd = -1;
    ctx->plane = 9;
    ctx->crtc = 42;
    ctx->count = 1;
    for (unsigned i = 0; i < NPROPS; ++i)
        ctx->props[i] = i + 1;
    Pool *pool = &ctx->pools[0];
    pool->width = 720;
    pool->height = 400;
    pool->dw = 1920;
    pool->dh = 1067;
    pool->dy = 6;
    pool->front = 1;
    for (unsigned i = 0; i < 2; ++i) {
        Buffer *buffer = &pool->buffers[i];
        buffer->fb = 101 + i;
        buffer->pitch = 720 * 4 + 64; /* exercise real DRM row padding */
        buffer->bytes = buffer->pitch * 400;
        buffer->map = mmap(NULL, buffer->bytes, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        assert(buffer->map != MAP_FAILED);
        memset(buffer->map, 0xa5, buffer->bytes);
    }
    assert(commit(ctx, pool, 0, DRM_MODE_ATOMIC_TEST_ONLY) == 0);
    assert(scanout_fb == 0 && present_count == 0);
    const size_t length = 720 * 400 * 4;
    uint8_t *pixels = malloc(length);
    assert(pixels);
    memset(pixels, 0x3c, length);
    for (unsigned i = 0; i < 3; ++i) {
        assert(kms_show(ctx, pixels, 720, 400, 720 * 4) == 0);
        assert(presented[i] == 101 + (i % 2));
    }
    for (unsigned i = 0; i < 2; ++i) {
        const Buffer *buffer = &pool->buffers[i];
        for (unsigned y = 0; y < 400; ++y) {
            const uint8_t *row = (const uint8_t *)buffer->map + y * buffer->pitch;
            for (unsigned x = 0; x < buffer->pitch; ++x)
                assert(row[x] == (x < 720 * 4 ? 0x3c : 0xa5));
        }
    }
    unsigned front = pool->front;
    reject_commit = true;
    assert(kms_show(ctx, pixels, 720, 400, 720 * 4) < 0);
    assert(pool->front == front && scanout_fb == 101);
    assert(strstr(kms_error(ctx), "present plane framebuffer"));
    reject_commit = false;
    assert(kms_show(ctx, pixels, 700, 400, 720 * 4) < 0);
    assert(kms_show(ctx, pixels, 720, 400, 1) < 0);
    free(pixels);
    assert(kms_close(ctx) == 0);
    assert(scanout_fb == 0 && removed == 2);
    puts("Native KMS tests passed: native-size uploads, pitch padding, double buffers, plane-only commits, failure handling and cleanup.");
    return 0;
}
