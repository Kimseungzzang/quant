package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.OffsetDateTime;

@Entity
@Table(name = "analysis_results")
@Getter
@NoArgsConstructor
public class AnalysisResult {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne(fetch = FetchType.LAZY)
    @JoinColumn(name = "run_id", nullable = false)
    private AnalysisRun run;

    @Column(nullable = false)
    private Integer rank;

    @Column(nullable = false, length = 20)
    private String stockCode;

    @Column(nullable = false, length = 100)
    private String stockName;

    @Column(nullable = false, length = 10)
    private String market;

    @Column(nullable = false, length = 20)
    private String horizon;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal currentPrice;

    @Column(precision = 8, scale = 4)
    private BigDecimal changePct;

    @Column(precision = 20, scale = 4)
    private BigDecimal tradingValue;

    @Column(precision = 8, scale = 4)
    private BigDecimal finalScore;

    @Column(precision = 8, scale = 4)
    private BigDecimal winRatePct;

    @Column(name = "backtest_return", precision = 8, scale = 4)
    private BigDecimal backtestReturn;

    @Column(precision = 8, scale = 4)
    private BigDecimal maxDrawdown;

    private Integer tradeCount;

    private OffsetDateTime createdAt;
}
