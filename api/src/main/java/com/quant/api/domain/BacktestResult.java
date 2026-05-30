package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.OffsetDateTime;

@Entity
@Table(name = "backtest_results")
@Getter
@NoArgsConstructor
public class BacktestResult {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private OffsetDateTime runAt;

    @Column(nullable = false, length = 20)
    private String stockCode;

    @Column(nullable = false, length = 100)
    private String stockName;

    @Column(nullable = false, length = 10)
    private String market;

    private Integer periodDays;
    private LocalDate startDate;
    private LocalDate endDate;

    @Column(precision = 18, scale = 4)
    private BigDecimal initialCapital;

    @Column(precision = 18, scale = 4)
    private BigDecimal finalCapital;

    @Column(precision = 8, scale = 4)
    private BigDecimal totalReturnPct;

    @Column(precision = 8, scale = 4)
    private BigDecimal winRatePct;

    @Column(precision = 8, scale = 4)
    private BigDecimal maxDrawdownPct;

    private Integer tradeCount;

    @Column(precision = 8, scale = 4)
    private BigDecimal avgHoldDays;

    @Column(precision = 8, scale = 4)
    private BigDecimal sharpeRatio;

    private Long analysisRunId;
}
