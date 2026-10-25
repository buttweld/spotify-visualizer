/* Hardware scaling on an unused overlay plane; the existing DRM owner keeps
 * its connector, primary plane, CRTC mode and HDMI audio clock unchanged.
 * All commits are plane-only and deliberately omit ALLOW_MODESET. */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

#define MAX_SIZES 3
#define NPROPS 11
static const char *prop_names[NPROPS] = {
    "FB_ID", "CRTC_ID", "SRC_X", "SRC_Y", "SRC_W", "SRC_H",
    "CRTC_X", "CRTC_Y", "CRTC_W", "CRTC_H", "SCALING_FILTER"
};

typedef struct {
    uint32_t fb, handle, pitch;
    uint64_t bytes;
    void *map;
} Buffer;

typedef struct {
    uint32_t width, height, dx, dy, dw, dh;
    Buffer buffers[2];
    unsigned front;
} Pool;

typedef struct {
    int fd;                       /* borrowed; never closed here */
    uint32_t crtc, plane, width, height;
    uint32_t props[NPROPS];
    uint64_t saved[NPROPS], filter;
    Pool pools[MAX_SIZES];
    unsigned count;
    bool active;
    char error[256];
} Context;

static int fail(Context *ctx, const char *operation) {
    int code = errno ? errno : EINVAL;
    snprintf(ctx->error, sizeof(ctx->error), "%s: %s", operation, strerror(code));
    return -code;
}

static void release_buffer(int fd, Buffer *buffer) {
    if (buffer->fb)
        drmModeRmFB(fd, buffer->fb);
    if (buffer->map && buffer->map != MAP_FAILED)
        munmap(buffer->map, buffer->bytes);
    if (buffer->handle) {
        struct drm_mode_destroy_dumb destroy = {.handle = buffer->handle};
        drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy);
    }
    memset(buffer, 0, sizeof(*buffer));
}

static int create_buffer(Context *ctx, Buffer *buffer, uint32_t w, uint32_t h) {
    struct drm_mode_create_dumb create = {.width = w, .height = h, .bpp = 32};
    if (drmIoctl(ctx->fd, DRM_IOCTL_MODE_CREATE_DUMB, &create))
        return fail(ctx, "allocate dumb buffer");
    buffer->handle = create.handle;
    buffer->pitch = create.pitch;
    buffer->bytes = create.size;
    uint32_t handles[4] = {create.handle}, pitches[4] = {create.pitch}, offsets[4] = {0};
    if (drmModeAddFB2(ctx->fd, w, h, DRM_FORMAT_XRGB8888,
                      handles, pitches, offsets, &buffer->fb, 0))
        return fail(ctx, "register XRGB8888 framebuffer");
    struct drm_mode_map_dumb mapping = {.handle = create.handle};
    if (drmIoctl(ctx->fd, DRM_IOCTL_MODE_MAP_DUMB, &mapping))
        return fail(ctx, "map dumb buffer");
    buffer->map = mmap(NULL, create.size, PROT_READ | PROT_WRITE, MAP_SHARED, ctx->fd, mapping.offset);
    if (buffer->map == MAP_FAILED)
        return fail(ctx, "mmap dumb buffer");
    memset(buffer->map, 0, create.size);
    return 0;
}

static int add_properties(Context *ctx, drmModeAtomicReq *request, const uint64_t *values) {
    for (unsigned i = 0; i < NPROPS; ++i) {
        if (!ctx->props[i])
            continue;
        if (drmModeAtomicAddProperty(request, ctx->plane, ctx->props[i], values[i]) < 0)
            return fail(ctx, "build plane transaction");
    }
    return 0;
}

static int commit(Context *ctx, Pool *pool, unsigned buffer, uint32_t flags) {
    uint64_t values[NPROPS] = {
        pool->buffers[buffer].fb, ctx->crtc, 0, 0,
        (uint64_t)pool->width << 16, (uint64_t)pool->height << 16,
        pool->dx, pool->dy, pool->dw, pool->dh, ctx->filter
    };
    drmModeAtomicReq *request = drmModeAtomicAlloc();
    if (!request) {
        errno = ENOMEM;
        return fail(ctx, "allocate plane transaction");
    }
    int result = add_properties(ctx, request, values);
    if (!result && drmModeAtomicCommit(ctx->fd, request, flags, NULL))
        result = fail(ctx, flags ? "test plane scaling" : "present plane framebuffer");
    drmModeAtomicFree(request);
    return result;
}

static bool plane_properties(Context *ctx, uint32_t plane) {
    drmModeObjectProperties *properties = drmModeObjectGetProperties(ctx->fd, plane, DRM_MODE_OBJECT_PLANE);
    if (!properties)
        return false;
    memset(ctx->props, 0, sizeof(ctx->props));
    memset(ctx->saved, 0, sizeof(ctx->saved));
    bool overlay = false, above_primary = true;
    ctx->filter = 0;
    for (uint32_t i = 0; i < properties->count_props; ++i) {
        drmModePropertyRes *property = drmModeGetProperty(ctx->fd, properties->props[i]);
        if (!property)
            continue;
        if (!strcmp(property->name, "type"))
            overlay = properties->prop_values[i] == DRM_PLANE_TYPE_OVERLAY;
        /* VC4 exposes primary zpos=0, overlay zpos>=1. Do not re-order planes. */
        if (!strcmp(property->name, "zpos"))
            above_primary = properties->prop_values[i] > 0;
        for (unsigned j = 0; j < NPROPS; ++j) {
            if (!strcmp(property->name, prop_names[j])) {
                ctx->props[j] = property->prop_id;
                ctx->saved[j] = properties->prop_values[i];
                if (j == NPROPS - 1) {
                    ctx->filter = properties->prop_values[i];
                    for (int k = 0; k < property->count_enums; ++k) {
                        if (strcasestr(property->enums[k].name, "nearest"))
                            ctx->filter = property->enums[k].value;
                    }
                }
            }
        }
        drmModeFreeProperty(property);
    }
    drmModeFreeObjectProperties(properties);
    for (unsigned i = 0; i < NPROPS - 1; ++i) {
        if (!ctx->props[i])
            return false;
    }
    return overlay && above_primary;
}

static int select_plane(Context *ctx) {
    drmModeRes *resources = drmModeGetResources(ctx->fd);
    if (!resources)
        return fail(ctx, "read DRM resources");
    int crtc_index = -1;
    for (int i = 0; i < resources->count_crtcs && i < 32; ++i) {
        if (resources->crtcs[i] == ctx->crtc)
            crtc_index = i;
    }
    drmModeFreeResources(resources);
    if (crtc_index < 0) {
        errno = ENODEV;
        return fail(ctx, "find active CRTC");
    }
    drmModePlaneRes *planes = drmModeGetPlaneResources(ctx->fd);
    if (!planes)
        return fail(ctx, "enumerate DRM planes");
    bool selected = false;
    for (uint32_t i = 0; i < planes->count_planes && !selected; ++i) {
        drmModePlane *plane = drmModeGetPlane(ctx->fd, planes->planes[i]);
        if (!plane)
            continue;
        bool available = !plane->crtc_id && !plane->fb_id &&
                         (plane->possible_crtcs & (1u << crtc_index));
        bool format = false;
        for (uint32_t j = 0; j < plane->count_formats; ++j)
            format |= plane->formats[j] == DRM_FORMAT_XRGB8888;
        uint32_t id = plane->plane_id;
        drmModeFreePlane(plane);
        if (!available || !format || !plane_properties(ctx, id))
            continue;
        ctx->plane = id;
        selected = true;
        for (unsigned j = 0; j < ctx->count; ++j) {
            /* Test every configured source resolution without changing scanout.
             * No connector, MODE_ID, ACTIVE or primary-plane properties occur. */
            if (commit(ctx, &ctx->pools[j], 0, DRM_MODE_ATOMIC_TEST_ONLY)) {
                selected = false;
                break;
            }
        }
    }
    drmModeFreePlaneResources(planes);
    if (selected)
        return 0;
    ctx->plane = 0;
    if (!ctx->error[0]) {
        errno = ENODEV;
        fail(ctx, "no unused XRGB8888 overlay plane supports all configured sizes");
    }
    return -ENODEV;
}

/* On failure, out still owns any partially allocated resources; close it. */
int kms_open(int fd, uint32_t crtc, uint32_t width, uint32_t height,
             const uint32_t *sizes, unsigned count, void **out) {
    *out = NULL;
    if (!width || !height || !count || count > MAX_SIZES)
        return -EINVAL;
    Context *ctx = calloc(1, sizeof(*ctx));
    if (!ctx)
        return -ENOMEM;
    *out = ctx;
    ctx->fd = fd;
    ctx->crtc = crtc;
    ctx->width = width;
    ctx->height = height;
    if (drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1) ||
        drmSetClientCap(fd, DRM_CLIENT_CAP_ATOMIC, 1))
        return fail(ctx, "enable atomic DRM plane access");
    drmModeCrtc *live = drmModeGetCrtc(fd, crtc);
    if (!live)
        return fail(ctx, "read active HDMI mode");
    bool matches = live->mode_valid && live->mode.hdisplay == width && live->mode.vdisplay == height;
    drmModeFreeCrtc(live);
    if (!matches) {
        errno = EINVAL;
        return fail(ctx, "active HDMI mode does not match framebuffer dimensions");
    }
    ctx->count = count;
    for (unsigned i = 0; i < count; ++i) {
        Pool *pool = &ctx->pools[i];
        pool->width = sizes[2*i];
        pool->height = sizes[2*i+1];
        if (!pool->width || !pool->height || pool->width > 16384 || pool->height > 16384) {
            errno = EINVAL;
            return fail(ctx, "invalid source resolution");
        }
        /* Integer aspect-preserving fit, including landscape letterboxing. */
        if ((uint64_t)width * pool->height <= (uint64_t)height * pool->width) {
            pool->dw = width;
            pool->dh = ((uint64_t)width * pool->height + pool->width/2) / pool->width;
        } else {
            pool->dh = height;
            pool->dw = ((uint64_t)height * pool->width + pool->height/2) / pool->height;
        }
        if (!pool->dw) pool->dw = 1;
        if (!pool->dh) pool->dh = 1;
        pool->dx = (width - pool->dw)/2;
        pool->dy = (height - pool->dh)/2;
        pool->front = 1;
        for (unsigned j = 0; j < 2; ++j) {
            if (create_buffer(ctx, &pool->buffers[j], pool->width, pool->height))
                return -ENOMEM;
        }
    }
    return select_plane(ctx);
}

const char *kms_error(void *context) {
    Context *ctx = context;
    return ctx && ctx->error[0] ? ctx->error : "could not initialize KMS scaling";
}

uint32_t kms_plane_id(void *context) {
    return ((Context *)context)->plane;
}

int kms_show(void *context, const uint8_t *pixels, uint32_t width, uint32_t height, size_t stride) {
    Context *ctx = context;
    if (!ctx || !ctx->plane || !pixels || stride < (size_t)width * 4)
        return -EINVAL;
    Pool *pool = NULL;
    for (unsigned i = 0; i < ctx->count; ++i) {
        if (ctx->pools[i].width == width && ctx->pools[i].height == height)
            pool = &ctx->pools[i];
    }
    if (!pool) {
        errno = EINVAL;
        return fail(ctx, "frame size was not tested at startup");
    }
    unsigned back = pool->front ^ 1;
    Buffer *buffer = &pool->buffers[back];
    for (uint32_t y = 0; y < height; ++y)
        memcpy((uint8_t *)buffer->map + (size_t)y * buffer->pitch,
               pixels + (size_t)y * stride, (size_t)width * 4);
    /* A synchronous commit waits for the flip, so the previous front buffer is
     * safe to reuse. No in-place writes into a buffer being scanned out. */
    int result = commit(ctx, pool, back, 0);
    if (!result) {
        pool->front = back;
        ctx->active = true;
    }
    return result;
}

int kms_close(void *context) {
    Context *ctx = context;
    if (!ctx)
        return 0;
    int result = 0;
    if (ctx->active) {
        drmModeAtomicReq *request = drmModeAtomicAlloc();
        if (request) {
            result = add_properties(ctx, request, ctx->saved);
            if (!result && drmModeAtomicCommit(ctx->fd, request, 0, NULL))
                result = fail(ctx, "restore unused overlay plane");
            drmModeAtomicFree(request);
        } else {
            result = -ENOMEM;
        }
    }
    for (unsigned i = 0; i < ctx->count; ++i)
        for (unsigned j = 0; j < 2; ++j)
            release_buffer(ctx->fd, &ctx->pools[i].buffers[j]);
    free(ctx);
    return result;
}
