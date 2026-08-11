package com.dbops.todo;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api")
public class LeakController {
    private static final Logger log = LoggerFactory.getLogger(LeakController.class);
    private static final int THRESHOLD = 10;
    private static final int CHUNK_BYTES = 1024 * 1024; // 1 MB per leaked buffer
    // BUG 3 (resource leak): retained buffers, never released. Grows heap over
    // time -> rising memory on host metrics + WARN/ERROR trickle in logs.
    private static final List<byte[]> LEAKED = new ArrayList<>();

    @GetMapping("/leak")
    public Map<String, Object> leak() {
        LEAKED.add(new byte[CHUNK_BYTES]);
        int open = LEAKED.size();
        if (open > THRESHOLD) {
            log.error("Resource leak threshold exceeded, open handles={}", open);
        } else {
            log.warn("Leaked resource, open handles={}", open);
        }
        return Map.of("open_handles", open, "leaked_bytes", (long) open * CHUNK_BYTES);
    }

    public static int openHandles() {
        return LEAKED.size();
    }

    public static void reset() {
        LEAKED.clear();
    }
}
