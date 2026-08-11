package com.dbops.todo;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class LeakControllerTest {
    @BeforeEach
    void reset() {
        LeakController.reset();
    }

    @Test
    void eachCallLeaksOneHandle() {
        LeakController c = new LeakController();
        c.leak();
        c.leak();
        assertThat(LeakController.openHandles()).isEqualTo(2);
    }

    @Test
    void crossingThresholdIsFlagged() {
        LeakController c = new LeakController();
        for (int i = 0; i < 11; i++) c.leak();
        assertThat(LeakController.openHandles()).isGreaterThan(10);
    }
}
