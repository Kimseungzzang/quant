package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;

@Entity
@Table(name = "analysis_runs")
@Getter
@NoArgsConstructor
public class AnalysisRun {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(nullable = false)
    private OffsetDateTime runAt;

    @Column(nullable = false, length = 10)
    private String market;

    @Column(nullable = false, length = 20)
    private String horizon;

    @Column(name = "top_n", nullable = false)
    private Integer topN;

    @Column(nullable = false, length = 20)
    private String status;

    private String errorMsg;

    @OneToMany(mappedBy = "run", fetch = FetchType.LAZY)
    private List<AnalysisResult> results = new ArrayList<>();
}
