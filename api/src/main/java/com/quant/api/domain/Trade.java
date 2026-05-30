package com.quant.api.domain;

import jakarta.persistence.*;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.math.BigDecimal;
import java.time.OffsetDateTime;
import java.util.UUID;

@Entity
@Table(name = "trades")
@Getter
@NoArgsConstructor
public class Trade {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private OffsetDateTime tradedAt;

    @Column(nullable = false, length = 20)
    private String stockCode;

    @Column(nullable = false, length = 100)
    private String stockName;

    @Column(nullable = false, length = 10)
    private String market;

    @Column(nullable = false, length = 4)
    private String side;          // BUY | SELL

    @Column(nullable = false)
    private Integer quantity;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal price;

    @Column(nullable = false, precision = 18, scale = 4)
    private BigDecimal amount;

    @Column(precision = 18, scale = 4)
    private BigDecimal commission;

    @Column(nullable = false, length = 10)
    private String mode;          // paper | live

    private String strategy;
    private String reason;

    @Column(precision = 18, scale = 4)
    private BigDecimal realizedPnl;

    @Column(precision = 8, scale = 4)
    private BigDecimal pnlPct;

    private UUID orderGroupId;
    private String kisOrderNo;
}
